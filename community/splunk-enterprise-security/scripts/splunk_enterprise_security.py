#!/usr/bin/env python3
"""splunk_enterprise_security CLI -- drives a Splunk Enterprise Security
Mission Control connector via the MCP api_proxy tool.

Subcommands:
  search-investigations  List/search investigations (GET .../investigations)
                          filtered by status, urgency, sensitivity,
                          disposition, owner, ids, or a create/update time
                          range. Emits grouped PSV with a ref for drill-down.
  get-investigation       Hydrate one investigation by GUID or display_id
                          (full detail card; no separate get-by-id endpoint --
                          this is list_investigations with `ids` set).
  get-investigation-notes List notes attached to an investigation or finding.
  search-findings         List/search findings (GET .../findings) filtered by
                          status, urgency, disposition, owner, rule_title,
                          finding_ids, or an earliest/latest time range.
  get-finding              Hydrate one finding by its event_id.
  get-risk-scores          Get risk scores for a risk entity (IP, user, ...).
  get-asset                Fetch one asset by its assets_by_str KV-store id.
                          There is no search-by-IP/hostname or list endpoint --
                          the id must already be known.
  get-identity             Fetch one identity by its KV-store id. Same
                          id-only limitation as get-asset.
  view-result              Re-print one row (full entity JSON) from a prior
                          search.
  refresh, ls, grep, upload-samples   Standard KG catalog/sample commands,
                          scoped to the findings and investigations surfaces
                          only -- assets and identity have no list endpoint
                          to walk.

This connector is read-only. It does not expose Splunk's core SPL/event
search -- that is out of scope for Enterprise Security's Mission Control API
and belongs to a separate core-Splunk connector if one is ever built.

The configured connector's base URL is the Mission Control API root
(https://<stack>/servicesNS/nobody/missioncontrol); the Authorization: Bearer
header is injected server-side from the connector's stored secret. This
skill is bound to one configured connector and resolves it automatically;
you don't pass a connector name.
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

SKILL_NAME = "splunk-enterprise-security"
PACKAGE = "splunk_enterprise_security"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "splunk_enterprise_security.py view-result"

# Context-budget rules (mirror the SKILL.md). A list call defaults to 5 rows;
# even an explicit "all"/"everything" request may not exceed the hard cap.
# The vendor's own page-size ceiling is 100 for every list endpoint below --
# this CLI imposes a stricter cap to protect the agent's context budget.
DEFAULT_LIMIT = 5
HARD_CAP = 25

# get_finding_by_id defaults to a 24-hour lookback window on `earliest` when
# the caller doesn't supply one -- a finding older than 24h silently returns
# 404 even though the id is valid. The CLI defaults wider to avoid that trap;
# --earliest still overrides it.
DEFAULT_GET_FINDING_EARLIEST = "-7d"

# Top-level fields shown for a list of investigations. list_investigations has
# no server-side `fields` filter (unlike get_findings), so every investigation
# comes back as a heavy object with nested response_plans/findings/
# consolidated_findings sub-objects. Trim to scalars for the list view; use
# get-investigation for the untrimmed record.
INVESTIGATION_LIST_FIELDS = (
    "investigation_id",
    "investigation_guid",
    "name",
    "status_name",
    "urgency",
    "sensitivity",
    "disposition_name",
    "owner",
    "count_findings",
    "risk_score",
    "create_time",
    "update_time",
)

DEFAULT_FINDING_FIELDS = (
    "event_id,rule_title,status_label,urgency,severity,disposition_label,"
    "owner,risk_object,risk_object_type,risk_score,notable_type,_time"
)

# REST surfaces sampled from a live list call for the KG field tier. Only
# findings and investigations have a list endpoint to walk -- assets and
# identity are id-only lookups (see get-asset/get-identity) and are
# deliberately excluded from the catalog.
INVESTIGATIONS_PATH = "/public/v2/investigations"
FINDINGS_PATH = "/public/v2/findings"
SURFACE_NAMES = ("investigations", "findings")

# Filterable query params per surface, used by the generic KG sample/lookup
# commands. Splunk ES exposes a fixed set of named filters per endpoint, not
# an arbitrary filter string -- a column outside this set can't be honored.
SURFACE_FILTER_PARAMS: dict[str, set[str]] = {
    "investigations": {"status", "urgency", "sensitivity", "disposition", "owner", "ids"},
    "findings": {"status", "urgency", "disposition", "owner", "rule_title", "finding_ids"},
}
# ids/finding_ids accept a comma-separated OR-list per the vendor docs; the
# other filters are documented as single-value equality only.
SURFACE_MULTI_VALUE_PARAMS: dict[str, str] = {
    "investigations": "ids",
    "findings": "finding_ids",
}


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _str_query(params: dict[str, Any]) -> dict[str, str]:
    """Drop unset values and stringify the rest for the proxy query dict."""
    return {k: str(v) for k, v in params.items() if v is not None and v != ""}


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _get_array(
    client: mcp.MCPClient,
    connector: str,
    path: str,
    *,
    query: dict[str, str],
    limit: int,
    verb: str,
    candidates_path: list[str] | None,
) -> list[dict[str, Any]]:
    """GET an endpoint that returns a bare JSON array (list_investigations,
    the risk-scores endpoint). Injects `limit` as a real query param -- the
    vendor paginates server-side; without it we'd fetch the vendor's own
    default page and only discard rows client-side."""
    query = dict(query)
    query.setdefault("limit", str(limit))
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query,
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ticketing",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    payload = _parse_json(r.body, verb)
    if not isinstance(payload, list):
        raise click.ClickException(f"{verb}: expected a JSON array, got {type(payload).__name__}")
    return [row for row in payload if isinstance(row, dict)][:limit]


def _get_items(
    client: mcp.MCPClient,
    connector: str,
    path: str,
    *,
    query: dict[str, str],
    limit: int,
    verb: str,
    candidates_path: list[str] | None,
) -> list[dict[str, Any]]:
    """GET an endpoint that wraps results as {"items": [...], "limit", "offset",
    "total"} (get_findings, get_notes_from_investigation). Injects `limit` as a
    real query param for the same reason as _get_array."""
    query = dict(query)
    query.setdefault("limit", str(limit))
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query,
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ticketing",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    payload = _parse_json(r.body, verb)
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise click.ClickException(f"{verb}: expected an 'items' array, got {body_preview(payload)}")
    return [row for row in items if isinstance(row, dict)][:limit]


def body_preview(payload: Any) -> str:
    return json.dumps(payload)[:300]


def _get_one(
    client: mcp.MCPClient,
    connector: str,
    path: str,
    *,
    query: dict[str, str],
    verb: str,
    candidates_path: list[str] | None,
) -> dict[str, Any]:
    """GET an endpoint that returns a single JSON object (get_finding_by_id,
    get_assets, get_identity)."""
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query,
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ticketing",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    return _parse_json(r.body, verb)


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
        click.echo("")
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


class _SplunkESSchemaProvider:
    """SchemaProvider for Splunk Enterprise Security: surface -> field.

    Both surfaces discover their field tier empirically from a one-row live
    sample -- there is no separate schema-description endpoint. Implements
    the contract in ``_lib/discover.py``.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Splunk Enterprise Security"
    TIERS_SINGULAR: tuple[str, ...] = ("Surface", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Surfaces", "Fields")
    # Fields come from a single sampled record (incomplete view -- e.g. a
    # finding without a response plan won't show current_response_plan_phase),
    # so the refresh walk unions rather than prunes them.
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
        if surface == "investigations":
            rows = _get_array(
                self._client,
                connector,
                INVESTIGATIONS_PATH,
                query={},
                limit=1,
                verb="sample investigations",
                candidates_path=["investigations"],
            )
        elif surface == "findings":
            rows = _get_items(
                self._client,
                connector,
                FINDINGS_PATH,
                query={},
                limit=1,
                verb="sample findings",
                candidates_path=["findings"],
            )
        else:
            return []
        if rows:
            return sorted(str(k) for k in rows[0].keys())
        return []


_EXAMPLES = """
Examples:

\b
  splunk_enterprise_security.py search-investigations --status New --urgency high --limit 5
  splunk_enterprise_security.py get-investigation --id ES-00001
  splunk_enterprise_security.py get-investigation-notes --id ES-00001
  splunk_enterprise_security.py search-findings --status "In Progress" --limit 5
  splunk_enterprise_security.py get-finding --id "b8684eb9-...@@notable@@b8684eb9..."
  splunk_enterprise_security.py get-risk-scores --entity bad_user@splunk.com
  splunk_enterprise_security.py get-asset --id 67bd956379ba456e810415c0
  splunk_enterprise_security.py view-result --ref <ref> --row 1
  splunk_enterprise_security.py grep --pattern urgency
  splunk_enterprise_security.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Splunk Enterprise Security (Mission Control) connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="search-investigations")
@click.option("--status", default=None, help="Status id or label, e.g. New, In Progress.")
@click.option("--urgency", default=None, help="informational | low | medium | high | critical | unknown.")
@click.option("--sensitivity", default=None, help="White | Green | Amber | Red | Unassigned.")
@click.option("--disposition", default=None, help="Disposition id or label, e.g. \"disposition:1,Undetermined\".")
@click.option("--owner", default=None, help="Investigation owner username.")
@click.option("--ids", default=None, help="Comma-separated GUIDs or display_ids to fetch specific investigations.")
@click.option("--create-time-min", default=None, help="Epoch seconds; investigations created at/after this time.")
@click.option("--create-time-max", default=None, help="Epoch seconds; investigations created at/before this time.")
@click.option("--update-time-min", default=None, help="Epoch seconds; investigations updated at/after this time.")
@click.option("--update-time-max", default=None, help="Epoch seconds; investigations updated at/before this time.")
@click.option("--sort", default="create_time:desc", show_default=True, help="e.g. create_time:asc,status:desc.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}; vendor cap is 100).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def search_investigations(
    ctx: click.Context,
    status: str | None,
    urgency: str | None,
    sensitivity: str | None,
    disposition: str | None,
    owner: str | None,
    ids: str | None,
    create_time_min: str | None,
    create_time_max: str | None,
    update_time_min: str | None,
    update_time_max: str | None,
    sort: str,
    limit: int,
    output_path: str | None,
) -> None:
    """List/search investigations. No server-side field trimming exists for
    this endpoint, so the list view shows a curated set of scalar columns --
    use get-investigation or view-result for the full record."""
    started = time.monotonic()
    query = _str_query(
        {
            "status": status,
            "urgency": urgency,
            "sensitivity": sensitivity,
            "disposition": disposition,
            "owner": owner,
            "ids": ids,
            "create_time_min": create_time_min,
            "create_time_max": create_time_max,
            "update_time_min": update_time_min,
            "update_time_max": update_time_max,
            "sort": sort,
        }
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        raw_rows = _get_array(
            client, connector, INVESTIGATIONS_PATH, query=query, limit=limit,
            verb="search investigations", candidates_path=["investigations"],
        )
    rows = [{k: r.get(k) for k in INVESTIGATION_LIST_FIELDS if k in r} for r in raw_rows]
    query_desc = json.dumps(query) if query else ""
    _emit_rows(ctx, rows, command="search-investigations", connector=connector, query=query_desc, catalog_path=["investigations"], started=started, output_path=output_path)


@cli.command(name="get-investigation")
@click.option("--id", "investigation_id", required=True, help="Investigation GUID or display_id (e.g. ES-00001).")
def get_investigation(investigation_id: str) -> None:
    """Hydrate one investigation by GUID or display_id (full untrimmed record).

    There is no dedicated get-by-id endpoint -- this calls list_investigations
    with `ids` set to a single value.
    """
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_array(
            client, connector, INVESTIGATIONS_PATH, query={"ids": investigation_id}, limit=1,
            verb="get investigation", candidates_path=["investigations"],
        )
    if not rows:
        raise click.ClickException(f"no investigation found for id {investigation_id!r}")
    click.echo(json.dumps(rows[0], indent=2))


@cli.command(name="get-investigation-notes")
@click.option("--id", "investigation_id", required=True, help="Investigation or finding GUID/display_id.")
@click.option("--search", "search_text", default=None, help="Keyword search over note title/content.")
@click.option("--type", "note_type", default=None, type=click.Choice(["Task", "Incident", "All"]), help="Filter by note source type.")
@click.option("--sort", default=None, help="create_time:1 | create_time:-1 | update_time:1 | update_time:-1.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}; vendor cap is 100).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def get_investigation_notes(
    ctx: click.Context,
    investigation_id: str,
    search_text: str | None,
    note_type: str | None,
    sort: str | None,
    limit: int,
    output_path: str | None,
) -> None:
    """List notes attached to an investigation or finding."""
    started = time.monotonic()
    query = _str_query({"search": search_text, "type": note_type, "sort": sort})
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_items(
            client, connector, f"{INVESTIGATIONS_PATH}/{investigation_id}/notes", query=query, limit=limit,
            verb="get investigation notes", candidates_path=None,
        )
    _emit_rows(ctx, rows, command="get-investigation-notes", connector=connector, query=investigation_id, catalog_path=None, started=started, output_path=output_path)


@cli.command(name="search-findings")
@click.option("--status", default=None, help="Status id or label, e.g. New, \"In Progress\".")
@click.option("--urgency", default=None, help="informational | low | medium | high | critical | unknown.")
@click.option("--disposition", default=None, help="Disposition label, e.g. \"True Positive - Suspicious Activity\".")
@click.option("--owner", default=None, help="Finding owner username.")
@click.option("--rule-title", default=None, help="Exact rule_title text.")
@click.option("--finding-ids", default=None, help="Comma-separated event_ids to fetch specific findings.")
@click.option("--earliest", default=None, help="Relative (-30m), epoch, or ISO 8601. No stated vendor default -- omitting it does not time-bound the search.")
@click.option("--latest", default=None, help="Relative, epoch, or ISO 8601. Vendor default is now if omitted.")
@click.option("--sort", default="create_time:desc", show_default=True, help="e.g. create_time:asc,status:desc.")
@click.option("--fields", default=DEFAULT_FINDING_FIELDS, show_default=False, help="Comma-separated fields to return.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}; vendor cap is 100).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def search_findings(
    ctx: click.Context,
    status: str | None,
    urgency: str | None,
    disposition: str | None,
    owner: str | None,
    rule_title: str | None,
    finding_ids: str | None,
    earliest: str | None,
    latest: str | None,
    sort: str,
    fields: str,
    limit: int,
    output_path: str | None,
) -> None:
    """List/search findings; emits grouped PSV with a ref for drill-down."""
    started = time.monotonic()
    query = _str_query(
        {
            "status": status,
            "urgency": urgency,
            "disposition": disposition,
            "owner": owner,
            "rule_title": rule_title,
            "finding_ids": finding_ids,
            "earliest": earliest,
            "latest": latest,
            "sort": sort,
            "fields": fields,
        }
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_items(
            client, connector, FINDINGS_PATH, query=query, limit=limit,
            verb="search findings", candidates_path=["findings"],
        )
    query_desc = json.dumps(query) if query else ""
    _emit_rows(ctx, rows, command="search-findings", connector=connector, query=query_desc, catalog_path=["findings"], started=started, output_path=output_path)


@cli.command(name="get-finding")
@click.option("--id", "event_id", required=True, help="Finding event_id, e.g. <guid>@@notable@@<hex>.")
@click.option("--earliest", default=DEFAULT_GET_FINDING_EARLIEST, show_default=True, help="Vendor default is the previous 24 hours if omitted -- a finding older than that silently 404s. This CLI defaults wider.")
@click.option("--latest", default=None, help="Relative, epoch, or ISO 8601. Vendor default is now if omitted.")
@click.option("--fields", default=None, help="Comma-separated fields to return; omit for the full record.")
def get_finding(event_id: str, earliest: str, latest: str | None, fields: str | None) -> None:
    """Hydrate one finding by its event_id."""
    query = _str_query({"earliest": earliest, "latest": latest, "fields": fields})
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        finding = _get_one(
            client, connector, f"{FINDINGS_PATH}/{event_id}", query=query,
            verb="get finding", candidates_path=["findings"],
        )
    click.echo(json.dumps(finding, indent=2))


@cli.command(name="get-risk-scores")
@click.option("--entity", required=True, help="Risk entity value, e.g. an IP address or username.")
@click.option("--entity-type", default=None, help="Comma-separated: user,system,hash_values,host_artifacts,tools,other.")
@click.option("--earliest", default=None, help="Relative, epoch, or ISO 8601. Omitting earliest/latest reads cached lookup-table risk scores instead of a live search.")
@click.option("--latest", default=None, help="Relative, epoch, or ISO 8601.")
@click.option("--sort", default="risk_score:desc", show_default=True, help="Sort on risk_score or entity_type only.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}; vendor cap is 100).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def get_risk_scores(
    ctx: click.Context,
    entity: str,
    entity_type: str | None,
    earliest: str | None,
    latest: str | None,
    sort: str,
    limit: int,
    output_path: str | None,
) -> None:
    """Get risk scores for a risk entity. Requires the mc_risk_score_write
    capability on the connector's token despite being a read-only call --
    a documented vendor naming quirk, not a typo in this connector."""
    started = time.monotonic()
    query = _str_query({"entity_type": entity_type, "earliest": earliest, "latest": latest, "sort": sort})
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _get_array(
            client, connector, f"/public/v2/risks/risk_scores/{entity}", query=query, limit=limit,
            verb="get risk scores", candidates_path=None,
        )
    _emit_rows(ctx, rows, command="get-risk-scores", connector=connector, query=entity, catalog_path=None, started=started, output_path=output_path)


@cli.command(name="get-asset")
@click.option("--id", "asset_id", required=True, help="assets_by_str KV-store id (e.g. 67bd956379ba456e810415c0). Must already be known -- there is no search-by-IP/hostname or list endpoint.")
def get_asset(asset_id: str) -> None:
    """Fetch one asset by its KV-store id."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        asset = _get_one(
            client, connector, f"/public/v2/assets/{asset_id}", query={},
            verb="get asset", candidates_path=None,
        )
    click.echo(json.dumps(asset, indent=2))


@cli.command(name="get-identity")
@click.option("--id", "identity_id", required=True, help="Identity KV-store id. Must already be known -- there is no search-by-email/username or list endpoint.")
def get_identity(identity_id: str) -> None:
    """Fetch one identity by its KV-store id."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        identity = _get_one(
            client, connector, f"/public/v2/identity/{identity_id}", query={},
            verb="get identity", candidates_path=None,
        )
    click.echo(json.dumps(identity, indent=2))


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
    return value.replace(",", "\\,")


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over the investigations or findings surface. Splunk ES
    exposes a fixed set of named query filters per endpoint (no arbitrary
    filter string like QRadar's AQL), so only columns in
    SURFACE_FILTER_PARAMS can be honored; anything else returns no rows."""
    if len(segments) != 1 or segments[0] not in SURFACE_FILTER_PARAMS:
        return []
    surface = segments[0]
    query: dict[str, str] = {}
    if column is not None and values:
        if column not in SURFACE_FILTER_PARAMS[surface]:
            return []
        if column == SURFACE_MULTI_VALUE_PARAMS.get(surface):
            query[column] = ",".join(_quote(v) for v in values)
        else:
            query[column] = values[0]
    if surface == "investigations":
        return _get_array(
            client, connector, INVESTIGATIONS_PATH, query=query, limit=limit,
            verb="sample investigations", candidates_path=["investigations"],
        )
    return _get_items(
        client, connector, FINDINGS_PATH, query=query, limit=limit,
        verb="sample findings", candidates_path=["findings"],
    )


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _SplunkESSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Splunk Enterprise Security",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Splunk ES connectors this skill owns; ``/<connector>/<surface>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Splunk ES connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Surface -> Field. Only investigations and findings are walkable\n-- assets and identity have no list endpoint and are id-only lookups.",
    refresh_docstring="Walk investigations/findings surface/field schema and commit catalog tiers to the KG.\n\nThe only ``splunk_enterprise_security`` command that hits the connector\nbackend for schema discovery; ``ls`` reads exclusively from the KG cache.\nEach surface is sampled a row at a time (fields unioned across samples).",
    grep_docstring="String-search this skill's Splunk ES connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact filter values the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent search (read from the in-container cache).",
    upload_from_file_help="Path to a file written by a `splunk_enterprise_security.py <search> --output ...` run.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /investigations/<field> or /findings/<field>).",
    upload_docstring="Verify-then-upload samples from a recent search.\n\nSource is either `--ref UUID` (the cache from a recent search) or\n`--from-file FILE` (a file produced by `--output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query`, the export cannot\nexceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
    upload_mismatch_msg="query in --query does not match the search the rows were fetched with (read-before-write check failed)",
    upload_limit_msg=lambda n: f"upload limit is {samples.MAX_UPLOAD_ROWS} rows, this cache holds {n}",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
