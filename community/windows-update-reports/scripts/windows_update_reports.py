#!/usr/bin/env python3
"""crogl-windows-update-reports CLI — drives a Windows Update for Business
reports connector via the MCP api_proxy tool (Azure Monitor Log Analytics
query API over a Log Analytics workspace).

Subcommands:
  query          KQL query over one of this connector's tables (UCClient,
                  UCClientUpdateStatus, ...). Emits grouped PSV with a ref
                  for drill-down.
  view-result     Drill into one row (full record JSON) from a prior query.
  upload-samples  Verify-then-upload sampled rows to the knowledge graph.
  refresh         Walk each connector's catalog (Table -> Field) into the KG.
  ls              List this skill's connectors' database schema by UNIX path.
  grep            String-search this skill's connectors' KG catalog.

Unlike the OData `$filter` connectors in this repo (Intune, Defender), this
one is not a REST resource list — the whole surface is one Azure Monitor Log
Analytics query endpoint (POST .../query with a KQL body) against a single
customer-owned workspace. There is no separate id -> hydrate `get`; a KQL
`where` clause covers point lookups the same way `query` covers search.

The configured custom connector's base URL should be the Log Analytics query
root for this tenant's workspace (https://api.loganalytics.azure.com/v1/workspaces/<id>).
Auth (an OAuth client_credentials Bearer, scope https://api.loganalytics.io/.default)
is applied server-side from the connector's stored secret.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import click

from _lib import (
    cache,
    env,
    instance,
    kgcommands,
    mcp,
    proxy,
    queryerror,
    samplecmds,
    samples,
)
from _lib.psv import Options, marshal_grouped

SKILL_NAME = "windows_update_reports"
PACKAGE = "windows_update_reports"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "windows_update_reports.py view-result"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). This connector has only one query
# surface (KQL over 8 tables), so one ceiling applies everywhere.
DEFAULT_LIMIT = 25
HARD_CAP = 100

# The 8 Windows Update for Business reports tables (Microsoft Learn:
# wufb-reports-schema). A shared Log Analytics workspace commonly carries
# other data sources too (Sentinel, other Azure diagnostics) -- this
# connector is scoped to WUfB's own tables only, enforced in
# _require_scoped_table below, the same way purview_dlp.py pins a fixed
# filter clause rather than trusting the caller's query to stay in scope.
TABLES: tuple[str, ...] = (
    "UCClient",
    "UCClientReadinessStatus",
    "UCClientUpdateStatus",
    "UCDeviceAlert",
    "UCDOAggregatedStatus",
    "UCDOStatus",
    "UCServiceUpdateStatus",
    "UCUpdateAlert",
)

_LEADING_TABLE_RE = re.compile(r"^\s*([A-Za-z_]\w*)")


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _leading_table(kql: str) -> str | None:
    m = _LEADING_TABLE_RE.match(kql)
    return m.group(1) if m else None


def _require_scoped_table(kql: str) -> None:
    """Reject a query whose leading table isn't one of this connector's 8
    WUfB tables (see TABLES comment above)."""
    table = _leading_table(kql)
    if table not in TABLES:
        raise click.BadParameter(
            f"query must start with one of this connector's tables -- got "
            f"{table!r}. Valid tables: {', '.join(TABLES)}.",
            param_hint="--query",
        )


def _query_table(
    client: mcp.MCPClient, connector: str, kql: str
) -> dict[str, list[Any]]:
    """POST one KQL query and return the primary result table's raw
    {"columns": [...], "rows": [...]}.

    The API returns full column metadata regardless of whether a column is
    null/absent on every row, unlike Graph's JSON-object sampling used by
    Intune/Defender -- so even a zero-row query still yields the true column
    list for schema discovery."""
    r = proxy.dispatch(
        client,
        connector,
        method="POST",
        path="/query",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"query": kql}),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="kql",
            verb="query",
            connector=connector,
            package=PACKAGE,
            query=kql,
            response=r,
            candidates_path=None,
            client=client,
        )
    payload = json.loads(r.body)
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, list) or not tables:
        return {"columns": [], "rows": []}
    primary = tables[0]
    if not isinstance(primary, dict):
        return {"columns": [], "rows": []}
    return {
        "columns": primary.get("columns") or [],
        "rows": primary.get("rows") or [],
    }


def _rows_from_table(tbl: dict[str, list[Any]]) -> list[dict[str, Any]]:
    columns = [
        c.get("name") for c in tbl["columns"] if isinstance(c, dict) and c.get("name")
    ]
    return [dict(zip(columns, row)) for row in tbl["rows"]]


def _search(
    client: mcp.MCPClient, connector: str, kql: str, limit: int
) -> list[dict[str, Any]]:
    """Run one KQL query capped to `limit` rows via an appended `| take`.

    A single request suffices -- the Log Analytics query API has no
    OData-style @odata.nextLink pagination.

    `limit` is clamped to HARD_CAP before use (the CLI's own click.IntRange
    already enforces this for `query`, but this function is also reachable
    directly from the shared `sample`/`lookup` commands, whose own --limit
    is not IntRange-bounded)."""
    limit = min(limit, HARD_CAP)
    tbl = _query_table(client, connector, f"{kql} | take {limit}")
    return _rows_from_table(tbl)[:limit]


class _WufbReportsSchemaProvider:
    """SchemaProvider for Windows Update for Business reports: table -> field.

    Log Analytics has no createmeta-style schema endpoint, so the field tier
    is discovered empirically via a one-row query -- but unlike Graph JSON
    sampling, the API's own `columns` metadata is present even when the
    sample returns zero rows, so this is a complete schema, not an
    incomplete one derived from whichever keys happened to be non-null.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Windows Update for Business reports"
    TIERS_SINGULAR: tuple[str, ...] = ("Table", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Tables", "Fields")
    LEAF_DISCOVERY_SAMPLED = True

    def __init__(self, client: mcp.MCPClient) -> None:
        self._client = client

    def list_my_connectors(self) -> list[str]:
        bound = instance.bound_connector_name(self._client)
        return [bound] if bound is not None else []

    def list_children(self, connector: str, tiers: list[str]) -> list[str]:
        return kgcommands.kg_list_children(self._client, connector, tiers)

    def discover_children(self, connector: str, tiers: list[str]) -> list[str]:
        if len(tiers) == 0:
            return list(TABLES)
        if len(tiers) == 1:
            return self._discover_fields(connector, tiers[0])
        return []

    def _discover_fields(self, connector: str, table: str) -> list[str]:
        if table not in TABLES:
            return []
        tbl = _query_table(self._client, connector, f"{table} | take 1")
        return sorted(
            str(c.get("name"))
            for c in tbl["columns"]
            if isinstance(c, dict) and c.get("name")
        )


_EXAMPLES = """
Examples:

\b
  windows_update_reports.py query --query "UCClient | where OSVersion contains 'Windows 11'" --limit 25
  windows_update_reports.py query --query "UCClientUpdateStatus | where ClientSubstate == 'RestartRequired'" --limit 25
  windows_update_reports.py query --query "UCUpdateAlert | where AlertStatus == 'Active'" --limit 25
  windows_update_reports.py view-result --ref <ref> --row 1
  windows_update_reports.py grep --pattern ReadinessStatus
  windows_update_reports.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Windows Update for Business reports connector via Azure Monitor Log Analytics. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--query",
    "kql",
    required=True,
    help="KQL query, e.g. \"UCClient | where OSVersion contains 'Windows 11'\". "
    "Must start with one of this connector's table names -- see `ls --path /`.",
)
@click.option(
    "--limit",
    required=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows to return, appended to the query as a trailing `| take` "
    f"(hard cap {HARD_CAP}). No default -- {DEFAULT_LIMIT} is a reasonable "
    "starting point for an analyst-facing question; see SKILL.md.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
@click.pass_context
def run_query(
    ctx: click.Context,
    kql: str,
    limit: int,
    output_path: str | None,
) -> None:
    """Run a KQL query and emit grouped PSV with a ref UUID for drill-down."""
    _require_scoped_table(kql)
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search(client, connector, kql, limit)

    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/query"
    table = _leading_table(kql) or ""

    if not rows:
        click.echo("")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            query=kql,
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
    cache.put(
        groups,
        ref=ref,
        producer=producer,
        connector=connector,
        query=kql,
        catalog_path=[table],
    )

    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=kql,
            ref=ref,
            rows=rows,
        )

    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        query=kql,
        row_count=len(rows),
        ref=ref,
        output=output_path,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query.")
@click.option(
    "--result-set",
    default=0,
    show_default=True,
    type=int,
    help="Result-set index (from the PSV footer).",
)
@click.option(
    "--row", required=True, type=int, help="Row number within the result set."
)
@click.pass_context
def view_result(ctx: click.Context, ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query.

    Rows are returned in full at query time, so this is a pure cache read —
    no second backend call needed."""
    try:
        record = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a WUfB table, in the canonical row shape
    upload-samples upserts. A path that is not a single known table returns
    []."""
    if len(segments) != 1 or segments[0] not in TABLES:
        return []
    table = segments[0]
    if column is not None:
        if not values:
            return []
        in_clause = " or ".join(f"{column} == '{v}'" for v in values)
        kql = f"{table} | where {in_clause}"
    else:
        kql = table
    return _search(client, connector, kql, limit)


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _WufbReportsSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Windows Update for Business reports",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists WUfB reports connectors this skill owns; ``/<connector>/<table>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Windows Update for Business reports connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Table -> Field. Fields are discovered empirically from a\none-row query of each table (via the query API's own column metadata).",
    refresh_docstring="Walk every configured WUfB reports connector's catalog (table -> field) and commit each tier to the KG via `commit_walk_tier`. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. The only producer path — `ls` reads from KG nodes exclusively.",
    grep_docstring="String-search this skill's Windows Update for Business reports connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact KQL query the samples came from (must match the source query).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `windows_update_reports.py query --output …`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /UCClient/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (KQL), the export\ncannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
