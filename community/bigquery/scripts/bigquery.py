#!/usr/bin/env python3
"""bigquery community connector CLI — query BigQuery datasets via Application Default Credentials.

Subcommands:
  query          Execute a SQL query and return results in grouped PSV format.
  ls             Schema discovery (UNIX-path) for this skill's BigQuery connectors.
  describe       Show the schema of a table.
  view-result    Fetch one full row from a prior query by ref.
  upload-samples Verify-and-upload a recent query's rows to the knowledge graph.
  refresh        Walk BigQuery catalogs and commit schema tiers to the KG.
  grep           Search this connector's KG catalog nodes.
  sample         (KG-internal) Fetch representative rows for edge learning.
  lookup         (KG-internal) Fetch rows matching specific column values.

Auth: Uses Google Cloud Application Default Credentials (ADC). On GCE VMs,
credentials are provided automatically via the instance metadata service.

The connector's project_id is extracted from the api_proxy base_url
(set during connector creation via the credential_schema binding).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Optional

import click

from _lib import (
    adoption,
    cache,
    dashboardcmds,
    env,
    instance,
    investigated_ticket,
    kgcommands,
    mcp,
    opentickets,
    queryerror,
    resolutiontime,
    samples,
    samplecmds,
)
from _lib.psv import Options, marshal_grouped

SKILL_NAME = "bigquery"
PACKAGE = "bigquery"
MAX_RESULT_SETS = 10
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
VIEW_TOOL_NAME = "bigquery.py view-result"

# Discover all datasets by default.
SCOPED_DATASETS: list[str] | None = None

# Dashboard tickets table is customer-configurable.  Read it from the
# BIGQUERY_DASHBOARD_TABLE env var (set by the operator or the agent
# container bootstrap).  When unset, dashboard cards degrade to
# "unavailable" rather than querying a non-existent table.
_DASHBOARD_TICKETS_TABLE = os.environ.get("BIGQUERY_DASHBOARD_TABLE", "")
_RESOLUTION_TIME_LIMIT = 5000
_OPEN_TICKETS_LIMIT = 5000

# Result-size ceiling for the interactive `query`/`sample`/`lookup` commands
# (CONTRIBUTING.md "Every list/query command has a code-enforced result-size
# ceiling"). Deliberately NOT applied inside `_run_query` itself -- that
# function is shared with the dashboard card producers above, which
# intentionally request up to _RESOLUTION_TIME_LIMIT/_OPEN_TICKETS_LIMIT
# (5000) rows for aggregation and are already considered safe at that size
# (see the pagination audit). Clamping `_run_query`'s own `limit` parameter
# to a smaller QUERY_HARD_CAP would silently truncate those dashboard
# queries -- the clamp instead lives at each interactive call site below.
QUERY_DEFAULT_LIMIT = 50
QUERY_HARD_CAP = 100
_ASSIGNEE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


# ---------------------------------------------------------------------------
# BQ client
# ---------------------------------------------------------------------------

_bq_client = None


def _get_client(project: str):
    """Return a cached BigQuery client."""
    global _bq_client
    try:
        from google.cloud import bigquery  # type: ignore[import-untyped]
    except ImportError:
        click.echo(
            "ERROR: google-cloud-bigquery is not installed. "
            "Run: pip install google-cloud-bigquery",
            err=True,
        )
        sys.exit(1)
    if _bq_client is None or _bq_client.project != project:
        _bq_client = bigquery.Client(project=project)
    return _bq_client


# ---------------------------------------------------------------------------
# MCP + connector resolution
# ---------------------------------------------------------------------------


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    client = mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])
    client.initialize()
    return client


def _resolve_project(client: mcp.MCPClient, connector: str) -> str:
    """Extract the GCP project ID from the connector's api_proxy base_url."""
    # First check for BIGQUERY_PROJECT environment variable
    project = os.environ.get("BIGQUERY_PROJECT")
    if project:
        return project
    
    # Check for local connector-summary.json or connector.json files
    for filename in ["connector-summary.json", "connector.json"]:
        filepath = os.path.join(os.path.dirname(__file__), "..", filename)
        if os.path.exists(filepath):
            try:
                with open(filepath) as f:
                    data = json.load(f)
                    # Handle both direct project_id and base_url formats
                    if "project_id" in data:
                        return data["project_id"]
                    elif "base_url" in data:
                        base_url = data["base_url"]
                        parts = base_url.rstrip("/").split("/")
                        if "projects" in parts:
                            idx = parts.index("projects")
                            if idx + 1 < len(parts):
                                return parts[idx + 1]
                    elif isinstance(data, dict) and "credential_schema" in data:
                        binding = data.get("credential_schema", {}).get("binding", {})
                        base_url = binding.get("base_url", "")
                        if base_url:
                            parts = base_url.rstrip("/").split("/")
                            if "projects" in parts:
                                idx = parts.index("projects")
                                if idx + 1 < len(parts):
                                    return parts[idx + 1]
            except Exception:
                pass
    
    # Try to get project from MCP client list_connectors
    try:
        res = client.call_tool("list_connectors", {})
        rows = res.get("connectors", []) if isinstance(res, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("name") == connector:
                base_url = row.get("base_url", "")
                # Handle case where base_url might be missing
                if not base_url:
                    continue
                parts = base_url.rstrip("/").split("/")
                if "projects" in parts:
                    idx = parts.index("projects")
                    if idx + 1 < len(parts):
                        return parts[idx + 1]
    except Exception:
        pass
    
    raise click.ClickException(
        f"could not resolve project_id for connector {connector!r}. "
        "Set BIGQUERY_PROJECT environment variable or ensure connector has a valid base_url."
    )


# ---------------------------------------------------------------------------
# Row serialization helpers
# ---------------------------------------------------------------------------


def _serialize_row(row_items: Any) -> dict[str, Any]:
    """Convert a BigQuery Row into a plain dict with JSON-safe values."""
    d = dict(row_items)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, bytes):
            d[k] = v.hex()
        elif isinstance(v, (list, dict)):
            d[k] = json.dumps(v, default=str)
    return d


# ---------------------------------------------------------------------------
# Core query execution
# ---------------------------------------------------------------------------


def _run_query(
    project: str,
    sql: str,
    *,
    dataset: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Execute a BigQuery SQL query and return rows as dicts.

    `limit` is appended as a trailing `LIMIT` clause ONLY if the substring
    "LIMIT" isn't already present in `sql` before its first `--` comment
    marker -- a crude, pre-existing check with gaps in both directions, not
    introduced or fixed by the CONTRIBUTING.md rule 4 retrofit this
    docstring was added for:

    - Over-matching: a LIMIT inside a subquery/CTE, or anywhere else in the
      statement, is treated as "already present," so no LIMIT is appended.
      This means a caller-supplied SQL string with its own explicit LIMIT
      (of any size, anywhere in the statement) is never overridden or
      capped by this function, `--limit`, or QUERY_HARD_CAP below --
      BigQuery honors whatever LIMIT the SQL itself declares. This is a
      deliberate, documented risk of exposing a raw-SQL interface (see
      SKILL.md Guardrails), not something this function can close without
      parsing arbitrary SQL.
    - Under-matching: `sql.upper().split("--")[0]` only inspects text
      before the FIRST `--`, so a leading `-- comment` line hides
      everything after it, including a real trailing LIMIT the query
      already has -- this function then appends a second LIMIT, producing
      invalid double-LIMIT SQL that BigQuery rejects with a syntax error.
      This is a separate, pre-existing bug (not a size-ceiling risk, since
      it fails loudly rather than under-limiting) -- flagged here rather
      than fixed as part of this retrofit; worth its own follow-up.

    This function has no result-size ceiling of its own by design -- see
    QUERY_HARD_CAP's comment above for why the clamp lives at each
    interactive call site instead of here.
    """
    from google.cloud import bigquery as bq  # type: ignore[import-untyped]

    bq_client = _get_client(project)
    job_config = bq.QueryJobConfig()
    if dataset:
        job_config.default_dataset = f"{project}.{dataset}"

    sql_stripped = sql.strip().rstrip(";")
    if "LIMIT" not in sql_stripped.upper().split("--")[0]:
        sql_stripped = f"{sql_stripped} LIMIT {limit}"

    try:
        query_job = bq_client.query(sql_stripped, job_config=job_config)
        results = query_job.result()
    except Exception as e:
        raise click.ClickException(f"BigQuery query error: {e}") from e

    rows: list[dict[str, Any]] = []
    for row in results:
        rows.append(_serialize_row(row.items()))
    return rows


def _dashboard_project(ctx: dashboardcmds.CardContext) -> str:
    return _resolve_project(ctx.client, ctx.connector)


def _require_dashboard_connector(ctx: dashboardcmds.CardContext) -> None:
    if not _DASHBOARD_TICKETS_TABLE:
        raise ValueError(
            "dashboard tickets table is not configured. Set the "
            "BIGQUERY_DASHBOARD_TABLE environment variable (or the "
            "dashboard_tickets_table connector credential field) to the "
            "fully-qualified BigQuery table backing the ticket dashboard "
            "(e.g. soc_tickets.incidents)."
        )


def _dashboard_assignee(email: str | None) -> str | None:
    if not email:
        return None
    candidate = email.strip().lower()
    if candidate.count("@") != 1:
        return None
    localpart = candidate.split("@", 1)[0]
    return localpart if _ASSIGNEE_RE.fullmatch(localpart) else None


def _assignee_exists(project: str, assignee: str) -> bool:
    rows = _run_query(
        project,
        (
            f"SELECT 1 AS found FROM `{project}.{_DASHBOARD_TICKETS_TABLE}` "
            f"WHERE LOWER(assigned_to) = '{assignee}' LIMIT 1"
        ),
        limit=1,
    )
    return bool(rows)


def _resolution_time_card(ctx: dashboardcmds.CardContext) -> dict[str, Any]:
    _require_dashboard_connector(ctx)
    project = _dashboard_project(ctx)
    now = datetime.now(timezone.utc)
    window_days = int(ctx.window.rstrip("d"))
    start_day = adoption.window_dates(window_days, now)[0].isoformat()

    where = [
        "resolved_at IS NOT NULL",
        f"DATE(resolved_at) >= DATE '{start_day}'",
    ]
    assignee = _dashboard_assignee(ctx.analyst)
    if ctx.analyst:
        if assignee is None or not _assignee_exists(project, assignee):
            return resolutiontime.build_card([], {}, window_days=window_days, now=now)
        where.append(f"LOWER(assigned_to) = '{assignee}'")

    rows = _run_query(
        project,
        (
            "SELECT ticket_id, created_at, resolved_at "
            f"FROM `{project}.{_DASHBOARD_TICKETS_TABLE}` "
            f"WHERE {' AND '.join(where)} ORDER BY resolved_at DESC LIMIT {_RESOLUTION_TIME_LIMIT}"
        ),
        limit=_RESOLUTION_TIME_LIMIT,
    )

    resolved: list[dict[str, Any]] = []
    for row in rows:
        ticket_id = row.get("ticket_id")
        created = investigated_ticket.parse_datetime(row.get("created_at"))
        resolved_at = investigated_ticket.parse_datetime(row.get("resolved_at"))
        if ticket_id and created and resolved_at:
            resolved.append(
                {"id": ticket_id, "created": created, "resolved": resolved_at}
            )

    card = resolutiontime.build_card(
        resolved,
        resolutiontime.analyzed_index(
            dashboardcmds.read_analyzed_tickets(ctx.client, ctx.connector)
        ),
        window_days=window_days,
        now=now,
    )
    if len(rows) >= _RESOLUTION_TIME_LIMIT:
        card["truncated"] = True
    return card


def _normalize_severity(value: Any) -> str:
    severity = value.strip().lower() if isinstance(value, str) else ""
    return severity if severity in opentickets.SEVERITY_ORDER else "unknown"


def _open_tickets_card(ctx: dashboardcmds.CardContext) -> dict[str, Any]:
    _require_dashboard_connector(ctx)
    project = _dashboard_project(ctx)
    where = ["resolved_at IS NULL"]
    assignee = _dashboard_assignee(ctx.analyst)
    if ctx.analyst:
        if assignee is None or not _assignee_exists(project, assignee):
            return opentickets.unlinked_card()
        where.append(f"LOWER(assigned_to) = '{assignee}'")

    rows = _run_query(
        project,
        (
            "SELECT ticket_id, title, severity, created_at "
            f"FROM `{project}.{_DASHBOARD_TICKETS_TABLE}` "
            f"WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT {_OPEN_TICKETS_LIMIT}"
        ),
        limit=_OPEN_TICKETS_LIMIT,
    )

    open_rows = [
        {
            "id": row.get("ticket_id"),
            "key": row.get("ticket_id"),
            "title": row.get("title"),
            "severity": _normalize_severity(row.get("severity")),
            "created": investigated_ticket.to_rfc3339(row.get("created_at")),
        }
        for row in rows
    ]
    open_ids = [row["id"] for row in open_rows if row.get("id")]
    analyzed_rows = (
        dashboardcmds.read_analyzed_tickets(
            ctx.client, ctx.connector, ticket_ids=open_ids
        )
        if open_ids
        else []
    )

    card = opentickets.build_card(open_rows, analyzed_rows)
    if len(rows) >= _OPEN_TICKETS_LIMIT:
        card["truncated"] = True
    return card


def _adoption_card(ctx: dashboardcmds.CardContext) -> dict[str, Any]:
    _require_dashboard_connector(ctx)
    project = _dashboard_project(ctx)
    now = datetime.now(timezone.utc)
    window_days = int(ctx.window.rstrip("d"))
    days = adoption.window_dates(window_days, now)
    start_day = days[0].isoformat()

    assignee_clause = ""
    assignee = _dashboard_assignee(ctx.analyst)
    if ctx.analyst:
        if assignee is None or not _assignee_exists(project, assignee):
            return opentickets.unlinked_card()
        assignee_clause = f" AND LOWER(assigned_to) = '{assignee}'"

    rows = _run_query(
        project,
        (
            "SELECT CAST(DATE(created_at) AS STRING) AS created_day, COUNT(*) AS created_count "
            f"FROM `{project}.{_DASHBOARD_TICKETS_TABLE}` "
            f"WHERE DATE(created_at) >= DATE '{start_day}'{assignee_clause} "
            "GROUP BY created_day ORDER BY created_day"
        ),
        limit=window_days,
    )
    created_by_day = {
        str(row.get("created_day")): int(row.get("created_count", 0))
        for row in rows
        if row.get("created_day")
    }

    analyzed = dashboardcmds.read_analyzed_tickets(
        ctx.client,
        ctx.connector,
        created_from=datetime(
            days[0].year, days[0].month, days[0].day, tzinfo=timezone.utc
        ).isoformat(),
        created_to=now.isoformat(),
    )
    return adoption.build_card(
        created_by_day, analyzed, window_days=window_days, now=now, analyst=ctx.analyst
    )


# ---------------------------------------------------------------------------
# KG SchemaProvider
# ---------------------------------------------------------------------------


class _BigQuerySchemaProvider:
    """SchemaProvider for BigQuery: dataset -> table -> field.

    Implements the contract in ``_lib/discover.py``. ``ls`` reads from the
    KG cache; ``refresh`` calls ``discover_children`` to enumerate datasets,
    tables, and fields from the live BigQuery API.

    Uses ``INFORMATION_SCHEMA`` for complete field enumeration, so
    ``LEAF_DISCOVERY_SAMPLED`` is False — genuinely removed fields are pruned.
    """

    PACKAGE = PACKAGE
    PLATFORM = "BigQuery"
    TIERS_SINGULAR: tuple[str, ...] = ("Dataset", "Table", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Datasets", "Tables", "Fields")
    # INFORMATION_SCHEMA gives a complete field list; prune missing fields.
    LEAF_DISCOVERY_SAMPLED = False

    def __init__(self, client: mcp.MCPClient, project: str) -> None:
        self._client = client
        self._project = project

    def list_my_connectors(self) -> list[str]:
        bound = instance.bound_connector_name(self._client)
        return [bound] if bound is not None else []

    def list_children(self, connector: str, tiers: list[str]) -> list[str]:
        return kgcommands.kg_list_children(self._client, connector, tiers)

    def discover_children(self, connector: str, tiers: list[str]) -> list[str]:
        """Real-time catalog walk for the ``refresh`` subcommand only.

        Tier 0 (root): list datasets (scoped to SCOPED_DATASETS if set).
        Tier 1 (dataset): list tables via BigQuery SDK.
        Tier 2 (dataset.table): list fields via table schema.
        Tier 3+: leaf reached, return [].
        """
        bq_client = _get_client(self._project)

        if len(tiers) == 0:
            # List datasets, optionally scoped
            all_datasets = list(bq_client.list_datasets())
            names = [ds.dataset_id for ds in all_datasets]
            if SCOPED_DATASETS is not None:
                names = [n for n in names if n in SCOPED_DATASETS]
            return sorted(names)

        if len(tiers) == 1:
            # List tables in dataset
            dataset_ref = f"{self._project}.{tiers[0]}"
            try:
                tables = list(bq_client.list_tables(dataset_ref))
            except Exception:
                return []
            return sorted(t.table_id for t in tables)

        if len(tiers) == 2:
            # List fields via table schema (complete enumeration)
            table_ref = f"{self._project}.{tiers[0]}.{tiers[1]}"
            try:
                tbl = bq_client.get_table(table_ref)
            except Exception:
                return []
            return sorted(field.name for field in tbl.schema)

        return []


# ---------------------------------------------------------------------------
# Edge-learning sample/lookup
# ---------------------------------------------------------------------------


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a BigQuery dataset.table.

    Called by the Go-native edge-learning pipeline via ``sample`` and ``lookup``
    subcommands. Returns rows in the same canonical shape as ``query`` output
    so KG field nodes and edge inner-joins align.

    A path that is not a dataset/table resource returns [].

    `limit` is clamped to QUERY_HARD_CAP (the CLI's own click.IntRange
    already enforces this for `query`, but this function is also reachable
    directly from the shared `sample`/`lookup` commands, whose own --limit
    is not IntRange-bounded).
    """
    if len(segments) < 2:
        return []

    limit = min(limit, QUERY_HARD_CAP)
    dataset = segments[0]
    table = segments[1]
    project = _resolve_project(client, connector)

    if column is not None:
        if not values:
            return []
        # Build WHERE clause with parameterized-style quoting
        quoted = ", ".join(f"'{_sql_escape(v)}'" for v in values)
        sql = (
            f"SELECT * FROM `{project}.{dataset}.{table}` "
            f"WHERE `{column}` IN ({quoted}) LIMIT {limit}"
        )
    else:
        sql = f"SELECT * FROM `{project}.{dataset}.{table}` LIMIT {limit}"

    try:
        rows = _run_query(project, sql, limit=limit)
    except click.ClickException:
        return []
    return rows


def _sql_escape(value: str) -> str:
    """Escape a string for BigQuery SQL single-quoted literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_EXAMPLES = """
Examples:

\b
  bigquery.py query --sql 'SELECT * FROM dataset.table' --limit 10
  bigquery.py view-result --ref <ref> --result-set 0 --row 0
  bigquery.py describe --table dataset.table
  bigquery.py grep --pattern alert
  bigquery.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """BigQuery connector — query and browse BigQuery datasets."""
    ctx.ensure_object(dict)
    ctx.obj["json_meta"] = json_meta
    client = _mcp_client()
    connector_name = instance.require_bound_connector(client)
    ctx.obj["client"] = client
    ctx.obj["connector"] = connector_name
    ctx.obj["project"] = _resolve_project(client, connector_name)


@cli.command()
@click.option("--sql", required=True, help="SQL query to execute")
@click.option("--dataset", default=None, help="Default dataset for unqualified tables")
@click.option(
    "--limit",
    default=QUERY_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, QUERY_HARD_CAP),
    help=f"Max rows to return (hard cap {QUERY_HARD_CAP}). Appended as a "
    "trailing SQL LIMIT only if --sql doesn't already contain one -- an "
    "explicit LIMIT in --sql is never overridden or capped; see SKILL.md.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
@click.pass_context
def query(
    ctx: click.Context,
    sql: str,
    dataset: Optional[str],
    limit: int,
    output_path: str | None,
) -> None:
    """Execute a SQL query and return results in grouped PSV format."""
    started = time.monotonic()
    project = ctx.obj["project"]
    connector = ctx.obj["connector"] or "bigquery"

    rows = _run_query(project, sql, dataset=dataset, limit=limit)

    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/query"

    if not rows:
        click.echo("(no results)")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            row_count=0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return

    body_text, groups = marshal_grouped(
        rows,
        SEARCH_OPTS,
        max_result_sets=MAX_RESULT_SETS,
        ref=ref,
        view_tool_name=VIEW_TOOL_NAME,
    )
    cache.put(groups, ref=ref, producer=producer, connector=connector, query=sql)

    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=sql,
            ref=ref,
            rows=[r for g in groups for r in g],
        )

    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        row_count=len(rows),
        ref=ref,
        output=output_path,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@cli.command("ls-datasets")
@click.option("--dataset", default=None, help="Dataset to list tables from")
@click.pass_context
def ls_datasets(ctx: click.Context, dataset: Optional[str]) -> None:
    """List datasets or tables in a BigQuery project (non-KG, direct SDK)."""
    project = ctx.obj["project"]
    bq_client = _get_client(project)

    if dataset:
        tables = list(bq_client.list_tables(f"{project}.{dataset}"))
        if not tables:
            click.echo(f"No tables in {project}.{dataset}")
            return
        click.echo(f"Tables in {project}.{dataset}:")
        for t in sorted(tables, key=lambda x: x.table_id):
            click.echo(f"  {t.table_id} ({t.table_type})")
    else:
        datasets = list(bq_client.list_datasets())
        if not datasets:
            click.echo(f"No datasets in project {project}")
            return
        click.echo(f"Datasets in {project}:")
        for ds in sorted(datasets, key=lambda x: x.dataset_id):
            click.echo(f"  {ds.dataset_id}")


@cli.command()
@click.option(
    "--table",
    required=True,
    help="Table: dataset.table or project.dataset.table",
)
@click.pass_context
def describe(ctx: click.Context, table: str) -> None:
    """Show the schema of a BigQuery table."""
    project = ctx.obj["project"]
    bq_client = _get_client(project)

    parts = table.split(".")
    if len(parts) == 2:
        table_ref = f"{project}.{parts[0]}.{parts[1]}"
    elif len(parts) == 3:
        table_ref = table
    else:
        click.echo(
            f"ERROR: Invalid table reference: {table}. Use dataset.table",
            err=True,
        )
        sys.exit(1)

    try:
        tbl = bq_client.get_table(table_ref)
    except Exception as e:
        click.echo(f"ERROR: {e}", err=True)
        sys.exit(1)

    click.echo(f"Table: {tbl.full_table_id}")
    click.echo(f"  Rows: {tbl.num_rows:,}")
    click.echo(f"  Size: {(tbl.num_bytes or 0) / (1024 * 1024):.1f} MB")
    click.echo(f"  Created: {tbl.created}")
    click.echo(f"  Modified: {tbl.modified}")
    if tbl.description:
        click.echo(f"  Description: {tbl.description}")
    click.echo("")
    click.echo("Schema:")
    for field in tbl.schema:
        mode = f" ({field.mode})" if field.mode != "NULLABLE" else ""
        desc = f" — {field.description}" if field.description else ""
        click.echo(f"  {field.name}: {field.field_type}{mode}{desc}")


@cli.command("view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query")
@click.option(
    "--result-set",
    required=True,
    type=int,
    help="Result-set index (from the PSV footer).",
)
@click.option(
    "--row", required=True, type=int, help="Row number within the result set."
)
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print one full row as JSON from a previously cached query."""
    try:
        record = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2, default=str))


# ---------------------------------------------------------------------------
# Register KG commands (ls, refresh, grep, upload-samples, sample, lookup)
# ---------------------------------------------------------------------------


def _provider_factory(client: mcp.MCPClient) -> _BigQuerySchemaProvider:
    """Build a BigQuerySchemaProvider for the bound connector instance."""
    project = _resolve_project(client, instance.require_bound_connector(client))
    return _BigQuerySchemaProvider(client, project)


samplecmds.add_commands(cli, _sample_rows)
dashboardcmds.add_commands(
    cli,
    producers={
        "resolution_time": _resolution_time_card,
        "open_tickets": _open_tickets_card,
        "adoption": _adoption_card,
    },
)
kgcommands.add_commands(
    cli,
    _provider_factory,
    skill_name=SKILL_NAME,
    platform_label="BigQuery",
    ls_path_help=(
        "UNIX-style absolute schema path. ``/`` lists BigQuery connectors "
        "this skill owns; ``/<connector>/<dataset>/<table>/<field>`` drills. "
        "Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); "
        "insert ``**`` immediately before the basename for recursive search."
    ),
    ls_docstring=(
        "Schema discovery for this skill's BigQuery connectors.\n\n"
        "Returns a JSON envelope (see ``_lib/discover.py``). The output goes\n"
        "to stdout; errors stay in the envelope as ``error`` (the process\n"
        "still exits zero so an aggregator can read the body)."
    ),
    refresh_docstring=(
        "Walk BigQuery catalogs in real time and commit catalog tiers to the KG.\n\n"
        "The only ``bigquery`` command that hits the BigQuery API for\n"
        "schema discovery; ``ls`` reads exclusively from the KG cache produced\n"
        "here. Uses ``INFORMATION_SCHEMA`` for complete field enumeration so\n"
        "removed fields are pruned. Scoped to the configured datasets.\n"
        "Resumable per-tier: each tier is its own MCP transaction."
    ),
    grep_docstring=(
        "String-search this skill's BigQuery connectors' KG catalog.\n\n"
        "Matches the pattern against each node's schema-element name, its\n"
        "LLM-assigned summary, and its tags. Reads the persisted knowledge graph\n"
        "(populated by `refresh` + the kg-learner describe pass), not the live API.\n"
        "Default match is a literal case-insensitive substring; use --regex for a\n"
        "POSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\n"
        "connector type at once, use `kg_learner.py grep`."
    ),
    upload_query_help="The exact SQL query the samples came from (must match the source).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `bigquery.py query --output ...`.",
    upload_catalog_path_help=(
        "POSIX-style catalog path the rows belong to "
        "(e.g. /<dataset>/<table>). Missing tiers are created server-side."
    ),
    upload_docstring=(
        "Verify-then-upload samples from a recent query.\n\n"
        "Source is either `--ref UUID` (the cache from a recent `query`) or\n"
        "`--from-file FILE` (a file produced by `query --output`). Exactly one is\n"
        "required. Both paths enforce a read-before-write check: the agent must\n"
        "have queried these exact rows with this exact `--sql`, the export\n"
        "cannot exceed the upload limit, and a content hash must match.\n\n"
        "On success the rows are persisted via the hidden `upsert_data_sample`\n"
        "MCP tool; the server resolves --catalog-path tier-by-tier, lazily creating\n"
        "any missing catalog entries."
    ),
)


if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
