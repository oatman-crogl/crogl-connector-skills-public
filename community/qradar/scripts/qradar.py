#!/usr/bin/env python3
"""qradar CLI -- drives an IBM QRadar SIEM custom connector via the MCP api_proxy tool.

Subcommands:
  search-offenses     List/search offenses (GET /api/siem/offenses) with a
                       SQL-like `filter`, `sort`, and `fields`. Emits grouped
                       PSV with a ref for drill-down.
  get-offense          Hydrate one offense by numeric ID (full detail card).
  aql                  Run an Ariel AQL query over events/flows. Async 3-step:
                       submit -> poll -> fetch results. Emits grouped PSV + ref.
  search-assets        Search the asset model by IP or hostname.
  list-log-sources     List configured log sources.
  list-offense-types   Resolve numeric offense_type IDs to names.
  list-closing-reasons List valid offense closing-reason IDs (needed to close).
  add-offense-note     WRITE: add a note to an offense (gated; requires --yes).
  close-offense        WRITE: close an offense with a closing reason (gated;
                       requires --yes; refuses to re-close a CLOSED offense).
  view-result          Re-print one row (full entity JSON) from a prior search.
  refresh, ls, grep, upload-samples   Standard KG catalog/sample commands.

The configured connector's base URL is the QRadar console root
(https://<host>); the `SEC` token header is injected server-side from the
connector's stored secret. Every call the CLI makes carries `Accept:
application/json` and `Version: 14.0`; pagination is a `Range: items=0-N`
header. This skill is bound to one configured connector and resolves it
automatically; you don't pass a connector name.
"""

from __future__ import annotations

import json
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

SKILL_NAME = "qradar"
PACKAGE = "qradar"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "qradar.py view-result"

# Supplied by the CLI on every call -- NOT stored in the connector config.
API_VERSION = "14.0"

# Context-budget rules (mirror the SKILL.md). A list call defaults to 5 rows;
# even an explicit "all"/"everything" request may not exceed the hard cap.
DEFAULT_LIMIT = 5
HARD_CAP = 25

# AQL is always async. Poll the search with a server-side long-poll
# (Prefer: wait=N) up to a bounded number of attempts before giving up.
AQL_POLL_ATTEMPTS = 15
AQL_POLL_WAIT_SECS = 10
AQL_TERMINAL = {"COMPLETED", "CANCELED", "ERROR"}

DEFAULT_OFFENSE_FIELDS = (
    "id,description,severity,magnitude,status,start_time,offense_type"
)
DEFAULT_OFFENSE_DRILL_FIELDS = (
    "id,description,severity,credibility,relevance,magnitude,status,assigned_to,"
    "offense_source,offense_type,start_time,last_updated_time,close_time,"
    "closing_reason_id,event_count,flow_count,device_count,category_count,"
    "categories,source_address_ids,destination_networks,source_network,domain_id"
)
DEFAULT_ASSET_FIELDS = (
    "id,hostnames(name),interfaces(ip_addresses(value)),properties(name,value)"
)

# REST surfaces are sampled from a live list call for the KG field tier.
REST_SURFACES: dict[str, str] = {
    "offenses": "/api/siem/offenses",
    "assets": "/api/asset_model/assets",
    "log_sources": "/api/config/event_sources/log_source_management/log_sources",
}

# AQL (Ariel) datasets expose a fixed, validated field set rather than a live
# sample -- their schema is stable and a `SELECT *` sample would drag in the
# large `payload` blob. Keep these in sync with references/.../qradar-deep-reference.md.
AQL_DATASETS: dict[str, tuple[str, ...]] = {
    "events": (
        "starttime", "endtime", "sourceip", "destinationip", "sourceport",
        "destinationport", "username", "logsourceid", "logsourcegroupids",
        "category", "qid", "eventcount", "magnitude", "credibility",
        "relevance", "severity", "devicetime", "payload", "protocolid",
        "domainid",
    ),
    "flows": (
        "starttime", "endtime", "sourceip", "destinationip", "sourceport",
        "destinationport", "sourcepackets", "destinationpackets", "sourcebytes",
        "destinationbytes", "protocolid", "applicationid", "firstpackettime",
        "lastpackettime",
    ),
}

SURFACE_NAMES = tuple(REST_SURFACES) + tuple(AQL_DATASETS)


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _range_upper(limit: int) -> int:
    """Upper bound for a `Range: items=0-N` header. Capped at HARD_CAP even if
    the caller asks for more -- unbounded fetches can return enormous pages."""
    return max(0, min(limit, HARD_CAP) - 1)


def _headers(range_upper: int | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Per-call headers. `Version` is required on every call and is NOT stored
    in the connector config. `Range` (pagination) belongs in headers, never in
    the query object. GETs deliberately omit `Content-Type` -- some builds
    reject it. Writes go through query params with an empty body, so they don't
    need it either."""
    h = {"Accept": "application/json", "Version": API_VERSION}
    if range_upper is not None:
        h["Range"] = f"items=0-{range_upper}"
    if extra:
        h.update(extra)
    return h


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _get_list(
    client: mcp.MCPClient,
    connector: str,
    path: str,
    *,
    query: dict[str, str] | None,
    limit: int,
    verb: str,
    candidates_path: list[str] | None,
) -> list[dict[str, Any]]:
    """GET a QRadar collection endpoint. These return a bare JSON array; the
    `Range` header bounds the page."""
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query or {},
        headers=_headers(_range_upper(limit)),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=(query or {}).get("filter"),
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    payload = _parse_json(r.body, verb)
    if not isinstance(payload, list):
        raise click.ClickException(f"{verb}: expected a JSON array, got {type(payload).__name__}")
    return [row for row in payload if isinstance(row, dict)][:limit]


def _emit_rows(
    ctx: click.Context,
    rows: list[dict[str, Any]],
    *,
    command: str,
    connector: str,
    query: str,
    catalog_path: list[str] | None,
    started: float,
    output_path: str | None,
) -> None:
    """Marshal rows to grouped PSV, cache them under a fresh ref for drill-down,
    optionally write a verifiable upload-samples export, and print."""
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/{command}"

    if not rows:
        query_note = f" for {query!r}" if query else ""
        click.echo(f"No {command} results (0 rows){query_note}.")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            command=command,
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
        query=query,
        catalog_path=catalog_path,
    )
    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=query,
            ref=ref,
            rows=rows,
        )
    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        command=command,
        row_count=len(rows),
        ref=ref,
        output=output_path,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _run_aql(
    client: mcp.MCPClient, connector: str, query_expression: str, limit: int
) -> list[dict[str, Any]]:
    """Submit an AQL query, poll to completion, and fetch a bounded page of
    results. AQL results are NEVER returned inline on the POST -- the three
    steps are mandatory."""
    # 1. Submit. query_expression is a QUERY parameter, not a body.
    sub = proxy.dispatch(
        client,
        connector,
        method="POST",
        path="/api/ariel/searches",
        query={"query_expression": query_expression},
        headers=_headers(),
    )
    if sub.status_code >= 400:
        raise queryerror.actionable(
            interface="aql",
            verb="submit AQL search",
            connector=connector,
            package=PACKAGE,
            query=query_expression,
            response=sub,
            candidates_path=None,
            client=client,
        )
    submitted = _parse_json(sub.body, "submit AQL search")
    search_id = submitted.get("search_id") if isinstance(submitted, dict) else None
    if not search_id:
        raise click.ClickException(f"AQL submit returned no search_id: {sub.body[:300]}")
    status = submitted.get("status")

    # 2. Poll with a server-side long-poll until terminal.
    attempts = 0
    while status not in AQL_TERMINAL and attempts < AQL_POLL_ATTEMPTS:
        poll = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=f"/api/ariel/searches/{search_id}",
            headers=_headers(extra={"Prefer": f"wait={AQL_POLL_WAIT_SECS}"}),
        )
        if poll.status_code >= 400:
            raise queryerror.actionable(
                interface="aql",
                verb="poll AQL search",
                connector=connector,
                package=PACKAGE,
                query=query_expression,
                response=poll,
                candidates_path=None,
                client=client,
            )
        polled = _parse_json(poll.body, "poll AQL search")
        status = polled.get("status") if isinstance(polled, dict) else None
        if status == "ERROR":
            msg = polled.get("error_messages") if isinstance(polled, dict) else None
            raise click.ClickException(f"AQL search failed: {msg or 'unknown error'}")
        attempts += 1

    if status != "COMPLETED":
        raise click.ClickException(
            f"AQL search {search_id} did not complete (last status {status!r} after "
            f"{attempts} polls). Narrow the time range or retry."
        )

    # 3. Fetch a bounded page of results. Results come back as a single-key
    # object keyed by the dataset name (events/flows).
    res = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=f"/api/ariel/searches/{search_id}/results",
        headers=_headers(_range_upper(limit)),
    )
    if res.status_code >= 400:
        raise queryerror.actionable(
            interface="aql",
            verb="fetch AQL results",
            connector=connector,
            package=PACKAGE,
            query=query_expression,
            response=res,
            candidates_path=None,
            client=client,
        )
    payload = _parse_json(res.body, "fetch AQL results")
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)][:limit]
    return []


def _get_offense(client: mcp.MCPClient, connector: str, offense_id: str, fields: str | None) -> dict[str, Any]:
    query = {"fields": fields} if fields else {}
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=f"/api/siem/offenses/{offense_id}",
        query=query,
        headers=_headers(),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb="get offense",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=["offenses"],
            client=client,
        )
    return _parse_json(r.body, "get offense")


class _QRadarSchemaProvider:
    """SchemaProvider for QRadar: surface -> field.

    REST surfaces (offenses, assets, log_sources) discover their field tier
    empirically from a one-row live sample. AQL datasets (events, flows) expose
    a fixed validated field set -- their schema is stable and sampling would
    pull the large `payload` blob. Implements the contract in ``_lib/discover.py``.
    """

    PACKAGE = PACKAGE
    PLATFORM = "QRadar"
    TIERS_SINGULAR: tuple[str, ...] = ("Surface", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Surfaces", "Fields")
    # REST fields come from a single sampled record (incomplete view), so the
    # refresh walk unions rather than prunes them.
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
            return list(SURFACE_NAMES)
        if len(tiers) == 1:
            return self._discover_fields(connector, tiers[0])
        return []

    def _discover_fields(self, connector: str, surface: str) -> list[str]:
        if surface in AQL_DATASETS:
            return sorted(AQL_DATASETS[surface])
        if surface in REST_SURFACES:
            rows = _get_list(
                self._client,
                connector,
                REST_SURFACES[surface],
                query={},
                limit=1,
                verb=f"sample {surface}",
                candidates_path=[surface],
            )
            if rows:
                return sorted(str(k) for k in rows[0].keys())
        return []


_EXAMPLES = """
Examples:

\b
  qradar.py search-offenses --filter "status = 'OPEN' AND magnitude >= 7" --limit 5
  qradar.py get-offense --id 12345
  qradar.py aql --query "SELECT sourceip, destinationip, eventcount FROM events WHERE sourceip = '<ip>' LAST 24 HOURS" --limit 25
  qradar.py search-assets --filter "hostnames(name = 'web01')" --limit 5
  qradar.py list-log-sources --limit 25
  qradar.py view-result --ref <ref> --row 1
  qradar.py grep --pattern magnitude
  qradar.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive an IBM QRadar SIEM connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="search-offenses")
@click.option(
    "--filter",
    "filter_str",
    default="",
    help="QRadar SQL-like filter, e.g. \"status = 'OPEN' AND magnitude >= 7\". "
    "AND/OR/NOT uppercase; strings single-quoted; timestamps epoch ms.",
)
@click.option("--sort", default="-start_time", show_default=True, help="Sort spec, e.g. -start_time or +magnitude.")
@click.option("--fields", default=DEFAULT_OFFENSE_FIELDS, show_default=False, help="Comma-separated fields to return.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def search_offenses(ctx: click.Context, filter_str: str, sort: str, fields: str, limit: int, output_path: str | None) -> None:
    """List/search offenses; emits grouped PSV with a ref for drill-down."""
    started = time.monotonic()
    query: dict[str, str] = {}
    if filter_str:
        query["filter"] = filter_str
    if sort:
        query["sort"] = sort
    if fields:
        query["fields"] = fields
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_list(client, connector, "/api/siem/offenses", query=query, limit=limit, verb="search offenses", candidates_path=["offenses"])
    _emit_rows(ctx, rows, command="search-offenses", connector=connector, query=filter_str, catalog_path=["offenses"], started=started, output_path=output_path)


@cli.command(name="get-offense")
@click.option("--id", "offense_id", required=True, help="Numeric offense ID.")
@click.option("--fields", default=DEFAULT_OFFENSE_DRILL_FIELDS, show_default=False, help="Comma-separated fields to return.")
def get_offense(offense_id: str, fields: str) -> None:
    """Hydrate one offense by ID and print the full JSON detail card."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        offense = _get_offense(client, connector, offense_id, fields)
    click.echo(json.dumps(offense, indent=2))


def _aql_command(
    ctx: click.Context, query_expression: str, limit: int, output_path: str | None, *, label: str
) -> None:
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _run_aql(client, connector, query_expression, limit)
    _emit_rows(ctx, rows, command=label, connector=connector, query=query_expression, catalog_path=None, started=started, output_path=output_path)


@cli.command(name="aql")
@click.option("--query", "query_expression", required=True, help="AQL query expression (SELECT ... FROM events|flows WHERE ... LAST N HOURS).")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max result rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def aql(ctx: click.Context, query_expression: str, limit: int, output_path: str | None) -> None:
    """Run an Ariel AQL query (async submit -> poll -> fetch) and emit grouped PSV."""
    _aql_command(ctx, query_expression, limit, output_path, label="aql")


@cli.command(name="query")
@click.option("--query", "query_expression", required=True, help="AQL query expression; QRadar's ad-hoc query language (Ariel/AQL).")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max result rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def query(ctx: click.Context, query_expression: str, limit: int, output_path: str | None) -> None:
    """Alias for `aql` -- QRadar's query language is Ariel/AQL over events & flows. For offenses use `search-offenses`."""
    _aql_command(ctx, query_expression, limit, output_path, label="query")


@cli.command(name="search-assets")
@click.option("--filter", "filter_str", default="", help="Asset filter, e.g. \"interfaces(ip_addresses(value = '<ip>'))\" or \"hostnames(name = 'web01')\".")
@click.option("--fields", default=DEFAULT_ASSET_FIELDS, show_default=False, help="Comma-separated fields to return.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def search_assets(ctx: click.Context, filter_str: str, fields: str, limit: int, output_path: str | None) -> None:
    """Search the asset model by IP or hostname; emits grouped PSV with a ref."""
    started = time.monotonic()
    query: dict[str, str] = {}
    if filter_str:
        query["filter"] = filter_str
    if fields:
        query["fields"] = fields
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_list(client, connector, "/api/asset_model/assets", query=query, limit=limit, verb="search assets", candidates_path=["assets"])
    _emit_rows(ctx, rows, command="search-assets", connector=connector, query=filter_str, catalog_path=["assets"], started=started, output_path=output_path)


@cli.command(name="list-log-sources")
@click.option("--filter", "filter_str", default="", help="Optional filter on the log-source collection.")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def list_log_sources(ctx: click.Context, filter_str: str, limit: int, output_path: str | None) -> None:
    """List configured log sources; emits grouped PSV with a ref."""
    started = time.monotonic()
    query: dict[str, str] = {"filter": filter_str} if filter_str else {}
    path = "/api/config/event_sources/log_source_management/log_sources"
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_list(client, connector, path, query=query, limit=limit, verb="list log sources", candidates_path=["log_sources"])
    _emit_rows(ctx, rows, command="list-log-sources", connector=connector, query=filter_str, catalog_path=["log_sources"], started=started, output_path=output_path)


@cli.command(name="list-offense-types")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def list_offense_types(limit: int) -> None:
    """List offense types (resolve numeric offense_type IDs to names)."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_list(client, connector, "/api/siem/offense_types", query={"fields": "id,name"}, limit=limit, verb="list offense types", candidates_path=None)
    click.echo(json.dumps(rows, indent=2))


@cli.command(name="list-closing-reasons")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def list_closing_reasons(limit: int) -> None:
    """List valid offense closing-reason IDs and text (needed to close an offense)."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_list(client, connector, "/api/siem/offense_closing_reasons", query={"fields": "id,text,is_deleted,is_reserved"}, limit=limit, verb="list closing reasons", candidates_path=None)
    click.echo(json.dumps(rows, indent=2))


@cli.command(name="add-offense-note")
@click.option("--id", "offense_id", required=True, help="Numeric offense ID.")
@click.option("--note", "note_text", required=True, help="Note text to add to the offense.")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def add_offense_note(offense_id: str, note_text: str, yes: bool) -> None:
    """WRITE: add a note to an offense. Gated -- requires --yes.

    `note_text` is a QUERY parameter (not a request body); the body is empty.
    """
    if not yes:
        raise click.ClickException(
            f"WRITE gated: would add a note to offense {offense_id}. "
            "Re-run with --yes only after the user has explicitly approved this action."
        )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="POST",
            path=f"/api/siem/offenses/{offense_id}/notes",
            query={"note_text": note_text},
            headers=_headers(),
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="querying",
                verb="add offense note",
                connector=connector,
                package=PACKAGE,
                response=r,
                candidates_path=["offenses"],
                client=client,
            )
    click.echo(json.dumps(_parse_json(r.body, "add offense note"), indent=2))


@cli.command(name="close-offense")
@click.option("--id", "offense_id", required=True, help="Numeric offense ID.")
@click.option("--closing-reason-id", "closing_reason_id", required=True, type=int, help="Closing-reason ID (see list-closing-reasons).")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def close_offense(offense_id: str, closing_reason_id: int, yes: bool) -> None:
    """WRITE: close an offense with a closing reason. Gated -- requires --yes.

    `status` and `closing_reason_id` are QUERY parameters (not a body). Refuses
    to re-close an offense already in CLOSED status.
    """
    if not yes:
        raise click.ClickException(
            f"WRITE gated: would close offense {offense_id} with closing_reason_id "
            f"{closing_reason_id}. Re-run with --yes only after the user has "
            "explicitly approved this action."
        )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        # Guardrail: never re-close a CLOSED offense.
        current = _get_offense(client, connector, offense_id, "id,status")
        if str(current.get("status")).upper() == "CLOSED":
            raise click.ClickException(f"offense {offense_id} is already CLOSED; nothing to do.")
        r = proxy.dispatch(
            client,
            connector,
            method="PATCH",
            path=f"/api/siem/offenses/{offense_id}",
            query={"status": "CLOSED", "closing_reason_id": str(closing_reason_id)},
            headers=_headers(),
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="querying",
                verb="close offense",
                connector=connector,
                package=PACKAGE,
                response=r,
                candidates_path=["offenses"],
                client=client,
            )
    click.echo(json.dumps(_parse_json(r.body, "close offense"), indent=2))


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior search.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="Row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior search (pure cache read)."""
    try:
        record = cache.get_row(ref, result_set, row - 1)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a QRadar surface. REST surfaces are sampled via
    the same list fetch the query commands use, so sampled columns line up with
    the KG field nodes. When a column/values pair is given, filter with an OR
    chain of single-quoted equalities (identity-bearing fields are strings). AQL
    datasets are not sampled here -- edge learning from live events is deferred."""
    if len(segments) != 1 or segments[0] not in REST_SURFACES:
        return []
    surface = segments[0]
    query: dict[str, str] = {}
    if column is not None and values:
        query["filter"] = " OR ".join(f"{column} = {_quote(v)}" for v in values)
    return _get_list(client, connector, REST_SURFACES[surface], query=query, limit=limit, verb=f"sample {surface}", candidates_path=[surface])


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _QRadarSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="QRadar",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists QRadar connectors this skill owns; ``/<connector>/<surface>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's QRadar connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Surface -> Field. REST surfaces (offenses, assets, log_sources)\ndiscover fields from a one-row live sample; AQL datasets (events, flows)\nexpose a fixed validated field set.",
    refresh_docstring="Walk QRadar surface/field schema and commit catalog tiers to the KG.\n\nThe only ``qradar`` command that hits the connector backend for schema\ndiscovery; ``ls`` reads exclusively from the KG cache. REST surfaces are\nsampled a row at a time (fields unioned across samples); AQL datasets\ncommit their fixed field set.",
    grep_docstring="String-search this skill's QRadar connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact filter/AQL the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent search (read from the in-container cache).",
    upload_from_file_help="Path to a file written by a `qradar.py <search> --output ...` run.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /offenses/<field> or /assets/<field>).",
    upload_docstring="Verify-then-upload samples from a recent search.\n\nSource is either `--ref UUID` (the cache from a recent search) or\n`--from-file FILE` (a file produced by `--output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query`, the export cannot\nexceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
    upload_mismatch_msg="query in --query does not match the search the rows were fetched with (read-before-write check failed)",
    upload_limit_msg=lambda n: f"upload limit is {samples.MAX_UPLOAD_ROWS} rows, this cache holds {n}",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
