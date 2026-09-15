#!/usr/bin/env python3
"""cisco_xdr_incidents CLI -- drives a Cisco XDR Incidents & Investigations
(Conure v2) connector via the MCP api_proxy tool.

Read-only scope only:
  * Incident search  -- GET /v2/incident/search
  * Incident count   -- GET /v2/incident/search/count
  * Incident get     -- GET /v2/incident/{incident-id}
  * Incident detail  -- GET /v2/incident/{incident-id}/<view>
  * Investigation detail -- GET /v2/investigation/{investigation-id}/<view>

Cisco XDR has no standalone "search investigations" endpoint -- investigations
are only reachable by id, discovered from an incident's `primary-investigation`
detail view (or already known to the caller). There is likewise no standalone
"get investigation" endpoint; `investigation-detail --view overview` is the
closest equivalent to a base record.

This connector is deliberately read-only and does not expose Casebook
management, incident/investigation creation or mutation, or the Report/
metrics family -- see SKILL.md's Scope section for why those were split out.

The configured connector's base URL is the tenant's region-specific Conure API
host (https://<api_host>); auth is an OAuth2 client_credentials Bearer token,
minted server-side from the stored client_id/client_password against the
tenant's region-specific token host. This skill is bound to one configured
connector and resolves it automatically; you don't pass a connector name.
"""

from __future__ import annotations

import json
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

SKILL_NAME = "cisco-xdr-incidents"
PACKAGE = "cisco-xdr-incidents"
PLATFORM_LABEL = "Cisco XDR"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "cisco_xdr_incidents.py view-result"
DEFAULT_LIMIT = 25
HARD_CAP = 100

INCIDENT_SEARCH_PATH = "/v2/incident/search"
INCIDENT_SEARCH_COUNT_PATH = "/v2/incident/search/count"
INCIDENT_PATH = "/v2/incident/{id}"
INCIDENT_DETAIL_PATH = "/v2/incident/{id}/{view}"
INVESTIGATION_DETAIL_PATH = "/v2/investigation/{id}/{view}"

RESOURCE_NAMES = ("incidents",)

# Documented `status` enum from GET /v2/incident/search (Conure v2 OpenAPI
# spec, definitions.IncidentStatusType), pulled live from
# https://conure.us.security.cisco.com/swagger.json -- not from vendor prose,
# which does not enumerate these anywhere.
INCIDENT_STATUS_VALUES = (
    "New",
    "New: Processing",
    "New: Presented",
    "Open",
    "Open: Reported",
    "Open: Investigating",
    "Open: Contained",
    "Open: Recovered",
    "Hold",
    "Hold: Internal",
    "Hold: External",
    "Hold: Legal",
    "Stalled",
    "Containment Achieved",
    "Restoration Achieved",
    "Incident Reported",
    "Closed",
    "Closed: Confirmed Threat",
    "Closed: False Positive",
    "Closed: Suspected",
    "Closed: Near-Miss",
    "Closed: Merged",
    "Closed: Under Review",
    "Closed: Other",
    "Rejected",
)
TLP_VALUES = ("amber", "green", "red", "white")
SEARCH_FIELD_VALUES = ("id", "short_description", "title", "source", "description")
SORT_BY_FIELDS = (
    "id",
    "language",
    "revision",
    "schema_version",
    "source",
    "source_uri",
    "timestamp",
    "title",
    "tlp",
)

INCIDENT_VIEWS = (
    "overview",
    "summary",
    "entities",
    "observables",
    "verdicts",
    "events",
    "mitre",
    "targets",
    "status",
    "indicators",
    "primary-investigation",
    "graph",
    "errors",
    "export",
    "linked-casebooks",
    "linked-incidents",
    "report",
)
INVESTIGATION_VIEWS = (
    "overview",
    "summary",
    "entities",
    "observables",
    "verdicts",
    "events",
    "indicators",
    "targets",
    "status",
    "graph",
)


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _get_json(
    client: mcp.MCPClient,
    connector: str,
    *,
    path: str,
    verb: str,
    query_desc: str,
    query: dict[str, str] | None,
    candidates_path: list[str] | None,
) -> Any:
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query or {},
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_desc,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    return _parse_json(r.body, verb)


def _query_desc(command: str, **kwargs: Any) -> str:
    return json.dumps({"command": command, **kwargs}, sort_keys=True, separators=(",", ":"))


def _emit_rows(
    rows: list[dict[str, Any]],
    *,
    command: str,
    connector: str,
    query_desc: str,
    catalog_path: list[str],
) -> str | None:
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/{command}"

    if not rows:
        click.echo(f"No {catalog_path[0]} results (0 rows).")
        return None

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
        query=query_desc,
        catalog_path=catalog_path,
    )
    click.echo(body_text)
    return ref


def _build_search_query(
    *,
    incident_id: str | None,
    status: str | None,
    tlp: str | None,
    from_: str | None,
    to: str | None,
    assignees: str | None,
    categories: str | None,
    confidence: str | None,
    detection_sources: str | None,
    discovery_method: str | None,
    high_impact: bool | None,
    intended_effect: str | None,
    promotion_method: str | None,
    query_text: str | None,
    simple_query: str | None,
    search_fields: str | None,
    source: str | None,
    language: str | None,
    sort_by: str | None,
    sort_order: str | None,
    offset: int,
    limit: int,
) -> dict[str, str]:
    """Assemble the GET /v2/incident/search query dict.

    Every key here is a documented parameter of the live Conure v2 OpenAPI
    spec's GET /v2/incident/search operation (pulled from
    https://conure.us.security.cisco.com/swagger.json). Cisco's own docs page
    does not enumerate these; the swagger descriptions for most of them are
    empty strings, so multi-value combination semantics (e.g. whether
    `assignees` accepts a comma-joined list or only ever matches a single
    value) are NOT vendor-confirmed -- pass one value at a time unless a live
    test proves otherwise.
    """
    params: dict[str, str] = {"offset": str(offset), "limit": str(limit)}
    optional = {
        "id": incident_id,
        "status": status,
        "tlp": tlp,
        "from": from_,
        "to": to,
        "assignees": assignees,
        "categories": categories,
        "confidence": confidence,
        "detection_sources": detection_sources,
        "discovery_method": discovery_method,
        "intended_effect": intended_effect,
        "promotion_method": promotion_method,
        "query": query_text,
        "simple_query": simple_query,
        "search_fields": search_fields,
        "source": source,
        "language": language,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    for key, value in optional.items():
        if value:
            params[key] = value
    if high_impact is not None:
        params["high_impact"] = "true" if high_impact else "false"
    return params


def _search_incidents(
    client: mcp.MCPClient,
    connector: str,
    query: dict[str, str],
) -> list[dict[str, Any]]:
    limit = int(query.get("limit", DEFAULT_LIMIT))
    payload = _get_json(
        client,
        connector,
        path=INCIDENT_SEARCH_PATH,
        verb="query incidents",
        query_desc=json.dumps(query, sort_keys=True),
        query=query,
        candidates_path=["incidents"],
    )
    if not isinstance(payload, list):
        raise click.ClickException("query incidents: expected a JSON array response")
    return [row for row in payload if isinstance(row, dict)][:limit]


def _discover_incident_fields(client: mcp.MCPClient, connector: str) -> list[str]:
    query = _build_search_query(
        incident_id=None,
        status=None,
        tlp=None,
        from_=None,
        to=None,
        assignees=None,
        categories=None,
        confidence=None,
        detection_sources=None,
        discovery_method=None,
        high_impact=None,
        intended_effect=None,
        promotion_method=None,
        query_text=None,
        simple_query=None,
        search_fields=None,
        source=None,
        language=None,
        sort_by="timestamp",
        sort_order="desc",
        offset=0,
        limit=1,
    )
    rows = _search_incidents(client, connector, query)
    if not rows:
        return []
    return sorted(str(k) for k in rows[0].keys())


class _CiscoXdrIncidentsSchemaProvider:
    """SchemaProvider for Cisco XDR Incidents: incidents -> field.

    Only `incidents` is walkable -- Conure has no "list/search investigations"
    endpoint, so investigation data is never independently enumerable and is
    excluded from the KG catalog. Fields are sampled from one search row
    (Conure has no dedicated schema-introspection endpoint for this API), so
    refresh unions sampled field names rather than treating absence as
    authoritative deletion.
    """

    PACKAGE = PACKAGE
    PLATFORM = PLATFORM_LABEL
    TIERS_SINGULAR: tuple[str, ...] = ("Resource", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Resources", "Fields")
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
            return list(RESOURCE_NAMES)
        if len(tiers) == 1 and tiers[0] == "incidents":
            return _discover_incident_fields(self._client, connector)
        return []


_EXAMPLES = """
Examples:

\b
  cisco_xdr_incidents.py query-incidents --status "Open: Investigating" --limit 25
  cisco_xdr_incidents.py query-incidents --high-impact --from 2026-08-01T00:00:00Z --limit 10
  cisco_xdr_incidents.py query-incidents --query "ransomware" --search-fields title,description --limit 10
  cisco_xdr_incidents.py count-incidents --status "Open: Investigating"
  cisco_xdr_incidents.py get-incident --id 7889a175-f43c-42ad-baac-ff55497c1730
  cisco_xdr_incidents.py incident-detail --id 7889a175-f43c-42ad-baac-ff55497c1730 --view summary
  cisco_xdr_incidents.py incident-detail --id 7889a175-f43c-42ad-baac-ff55497c1730 --view report
  cisco_xdr_incidents.py incident-detail --id 7889a175-f43c-42ad-baac-ff55497c1730 --view primary-investigation
  cisco_xdr_incidents.py investigation-detail --id <investigation-id> --view overview
  cisco_xdr_incidents.py view-result --ref <ref> --row 1
  cisco_xdr_incidents.py grep --pattern severity
  cisco_xdr_incidents.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
def cli() -> None:
    """Drive a Cisco XDR Incidents & Investigations connector. Run `<command> --help` for details."""


@cli.command(name="query-incidents")
@click.option("--id", "incident_id", default=None, help="Exact incident id to match.")
@click.option(
    "--status",
    type=click.Choice(INCIDENT_STATUS_VALUES),
    default=None,
    help="Incident status (documented Conure v2 enum).",
)
@click.option("--tlp", type=click.Choice(TLP_VALUES), default=None, help="Traffic Light Protocol level.")
@click.option("--from", "from_", default=None, help="ISO 8601 start of the time window (e.g. 2026-08-01T00:00:00Z).")
@click.option("--to", default=None, help="ISO 8601 end of the time window.")
@click.option("--assignees", default=None, help="Assignee to match. Multi-value/OR behavior is not vendor-confirmed -- pass one value at a time unless proven otherwise live.")
@click.option("--categories", default=None, help="Incident category to match. Multi-value behavior not vendor-confirmed.")
@click.option("--confidence", default=None, help="Confidence value to match.")
@click.option("--detection-sources", "detection_sources", default=None, help="Detection source to match.")
@click.option("--discovery-method", "discovery_method", default=None, help="Discovery method to match.")
@click.option("--high-impact/--no-high-impact", "high_impact", default=None, help="Filter on the high_impact flag.")
@click.option("--intended-effect", "intended_effect", default=None, help="Intended-effect value to match.")
@click.option("--promotion-method", "promotion_method", default=None, help="Promotion-method value to match.")
@click.option("--query", "query_text", default=None, help="Free-text query (Conure's own `query` param). Grammar is not documented by the vendor -- treat as opaque and prefer --simple-query or explicit filters when unsure.")
@click.option("--simple-query", "simple_query", default=None, help="Simple-query-format free text (Conure's `simple_query` param).")
@click.option(
    "--search-fields",
    default=None,
    help=f"Comma-separated fields --query/--simple-query search over. Documented values: {', '.join(SEARCH_FIELD_VALUES)}.",
)
@click.option("--source", default=None, help="Source system to match.")
@click.option("--language", default=None, help="Language code to match.")
@click.option(
    "--sort-by",
    default=None,
    help=f"Comma-separated sort fields, each optionally suffixed :asc or :desc (e.g. timestamp:desc). Documented sortable fields: {', '.join(SORT_BY_FIELDS)}.",
)
@click.option("--sort-order", type=click.Choice(["asc", "desc"]), default=None, help="Top-level sort_order param. Vendor docs do not clarify how this interacts with a per-field :asc/:desc suffix on --sort-by when both are given.")
@click.option("--offset", default=0, show_default=True, type=click.IntRange(0), help="Zero-based starting offset.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (client-side cap {HARD_CAP} -- the vendor does not document a hard server-side max).")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def query_incidents(
    incident_id: str | None,
    status: str | None,
    tlp: str | None,
    from_: str | None,
    to: str | None,
    assignees: str | None,
    categories: str | None,
    confidence: str | None,
    detection_sources: str | None,
    discovery_method: str | None,
    high_impact: bool | None,
    intended_effect: str | None,
    promotion_method: str | None,
    query_text: str | None,
    simple_query: str | None,
    search_fields: str | None,
    source: str | None,
    language: str | None,
    sort_by: str | None,
    sort_order: str | None,
    offset: int,
    limit: int,
    output_path: str | None,
) -> None:
    """Search incidents (GET /v2/incident/search) and emit grouped PSV with a ref UUID for drill-down."""
    query = _build_search_query(
        incident_id=incident_id,
        status=status,
        tlp=tlp,
        from_=from_,
        to=to,
        assignees=assignees,
        categories=categories,
        confidence=confidence,
        detection_sources=detection_sources,
        discovery_method=discovery_method,
        high_impact=high_impact,
        intended_effect=intended_effect,
        promotion_method=promotion_method,
        query_text=query_text,
        simple_query=simple_query,
        search_fields=search_fields,
        source=source,
        language=language,
        sort_by=sort_by,
        sort_order=sort_order,
        offset=offset,
        limit=limit,
    )
    query_desc = json.dumps(query, sort_keys=True)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_incidents(client, connector, query)
    ref = _emit_rows(
        rows,
        command="query-incidents",
        connector=connector,
        query_desc=query_desc,
        catalog_path=["incidents"],
    )
    if output_path and rows and ref is not None:
        samples.write_export(
            output_path,
            producer=f"{SKILL_NAME}/query-incidents",
            connector=connector,
            query=query_desc,
            ref=ref,
            rows=rows,
        )


@cli.command(name="count-incidents")
@click.option("--id", "incident_id", default=None, help="Exact incident id to match.")
@click.option("--status", type=click.Choice(INCIDENT_STATUS_VALUES), default=None, help="Incident status.")
@click.option("--tlp", type=click.Choice(TLP_VALUES), default=None, help="Traffic Light Protocol level.")
@click.option("--from", "from_", default=None, help="ISO 8601 start of the time window.")
@click.option("--to", default=None, help="ISO 8601 end of the time window.")
@click.option("--assignees", default=None, help="Assignee to match.")
@click.option("--categories", default=None, help="Incident category to match.")
@click.option("--confidence", default=None, help="Confidence value to match.")
@click.option("--detection-sources", "detection_sources", default=None, help="Detection source to match.")
@click.option("--discovery-method", "discovery_method", default=None, help="Discovery method to match.")
@click.option("--high-impact/--no-high-impact", "high_impact", default=None, help="Filter on the high_impact flag.")
@click.option("--intended-effect", "intended_effect", default=None, help="Intended-effect value to match.")
@click.option("--promotion-method", "promotion_method", default=None, help="Promotion-method value to match.")
@click.option("--query", "query_text", default=None, help="Free-text query (Conure's own `query` param).")
@click.option("--simple-query", "simple_query", default=None, help="Simple-query-format free text.")
@click.option("--search-fields", default=None, help=f"Comma-separated fields --query/--simple-query search over. Documented values: {', '.join(SEARCH_FIELD_VALUES)}.")
@click.option("--source", default=None, help="Source system to match.")
@click.option("--language", default=None, help="Language code to match.")
def count_incidents(
    incident_id: str | None,
    status: str | None,
    tlp: str | None,
    from_: str | None,
    to: str | None,
    assignees: str | None,
    categories: str | None,
    confidence: str | None,
    detection_sources: str | None,
    discovery_method: str | None,
    high_impact: bool | None,
    intended_effect: str | None,
    promotion_method: str | None,
    query_text: str | None,
    simple_query: str | None,
    search_fields: str | None,
    source: str | None,
    language: str | None,
) -> None:
    """Count incidents matching a filter (GET /v2/incident/search/count) without fetching rows.

    Accepts the same filter flags as `query-incidents` (minus pagination/sort,
    which this command omits deliberately -- the vendor docs don't state
    whether `offset`/`limit`/`sort_by` affect the returned count, so this
    command sends filters only). Cheaper than `query-incidents` for an agent
    that just needs to gauge result size before deciding whether/how to
    narrow a search.
    """
    query = _build_search_query(
        incident_id=incident_id,
        status=status,
        tlp=tlp,
        from_=from_,
        to=to,
        assignees=assignees,
        categories=categories,
        confidence=confidence,
        detection_sources=detection_sources,
        discovery_method=discovery_method,
        high_impact=high_impact,
        intended_effect=intended_effect,
        promotion_method=promotion_method,
        query_text=query_text,
        simple_query=simple_query,
        search_fields=search_fields,
        source=source,
        language=language,
        sort_by=None,
        sort_order=None,
        offset=0,
        limit=1,
    )
    query.pop("offset", None)
    query.pop("limit", None)
    query_desc = json.dumps(query, sort_keys=True)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get_json(
            client,
            connector,
            path=INCIDENT_SEARCH_COUNT_PATH,
            verb="count incidents",
            query_desc=query_desc,
            query=query,
            candidates_path=["incidents"],
        )
    if not isinstance(payload, int):
        raise click.ClickException("count incidents: expected an integer response")
    click.echo(str(payload))


@cli.command(name="get-incident")
@click.option("--id", "incident_id", required=True, help="Incident id (UUID or short_id).")
def get_incident(incident_id: str) -> None:
    """Hydrate one incident's base record (GET /v2/incident/{id}) as full JSON."""
    query_desc = _query_desc("get-incident", incident_id=incident_id)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get_json(
            client,
            connector,
            path=INCIDENT_PATH.format(id=incident_id),
            verb="get incident",
            query_desc=query_desc,
            query=None,
            candidates_path=["incidents"],
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="incident-detail")
@click.option("--id", "incident_id", required=True, help="Incident id (UUID or short_id).")
@click.option(
    "--view",
    type=click.Choice(INCIDENT_VIEWS),
    required=True,
    help="Which detail view to hydrate. See SKILL.md's Resources table for what each view returns.",
)
def incident_detail(incident_id: str, view: str) -> None:
    """Hydrate one detail view of an incident (GET /v2/incident/{id}/<view>) as full JSON.

    Note: the vendor's own OpenAPI spec labels the `verdicts` view's summary
    as "Returns a list of events linked to this incident" -- this looks like a
    copy-paste artifact in Cisco's docs (the response schema is a distinct
    verdict-shaped array, not an event array). Treat the response schema, not
    that summary text, as authoritative.
    """
    query_desc = _query_desc("incident-detail", incident_id=incident_id, view=view)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get_json(
            client,
            connector,
            path=INCIDENT_DETAIL_PATH.format(id=incident_id, view=view),
            verb=f"incident {view}",
            query_desc=query_desc,
            query=None,
            candidates_path=["incidents"],
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="investigation-detail")
@click.option("--id", "investigation_id", required=True, help="Investigation id -- get one from `incident-detail --view primary-investigation` or a prior investigation reference.")
@click.option(
    "--view",
    type=click.Choice(INVESTIGATION_VIEWS),
    required=True,
    help="Which detail view to hydrate. There is no standalone 'get investigation' endpoint -- --view overview is the closest equivalent to a base record.",
)
def investigation_detail(investigation_id: str, view: str) -> None:
    """Hydrate one detail view of an investigation (GET /v2/investigation/{id}/<view>) as full JSON."""
    query_desc = _query_desc("investigation-detail", investigation_id=investigation_id, view=view)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get_json(
            client,
            connector,
            path=INVESTIGATION_DETAIL_PATH.format(id=investigation_id, view=view),
            verb=f"investigation {view}",
            query_desc=query_desc,
            query=None,
            candidates_path=None,
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query-incidents call.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="1-based row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query-incidents call."""
    try:
        record = cache.get_row(ref, result_set, row - 1)
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
    """KG sample/lookup over incidents. Investigations are not sample-able --
    Conure has no search/list endpoint for them, so there is no way to
    enumerate a sample independent of an already-known incident/investigation
    id."""
    if len(segments) != 1 or segments[0] != "incidents":
        return []
    query = _build_search_query(
        incident_id=(values[0] if column == "id" and len(values) == 1 else None),
        status=None,
        tlp=None,
        from_=None,
        to=None,
        assignees=None,
        categories=None,
        confidence=None,
        detection_sources=None,
        discovery_method=None,
        high_impact=None,
        intended_effect=None,
        promotion_method=None,
        query_text=None,
        simple_query=None,
        search_fields=None,
        source=None,
        language=None,
        sort_by="timestamp",
        sort_order="desc",
        offset=0,
        limit=limit,
    )
    if column is not None and column != "id":
        # The search endpoint has no generic field=value lookup beyond the
        # documented named params above; unsupported columns yield no sample
        # rather than silently ignoring the requested column.
        return []
    if column == "id" and not values:
        return []
    return _search_incidents(client, connector, query)


samplecmds.add_commands(cli, _sample_rows)

kgcommands.add_commands(
    cli,
    lambda client: _CiscoXdrIncidentsSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label=PLATFORM_LABEL,
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Cisco XDR connectors this skill owns; ``/<connector>/incidents/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Cisco XDR connectors' schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: incidents -> Field. Investigations are not walkable -- Conure has\nno search/list endpoint for them. Fields are sampled from one search row,\nnot discovered from a dedicated schema endpoint.",
    refresh_docstring="Walk the Cisco XDR incidents catalog in real time and commit catalog tiers to the KG.\n\nThe only ``cisco-xdr-incidents`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier. Because fields are sampled from one row, refresh unions\nfield names rather than treating absence as authoritative deletion.",
    grep_docstring="String-search this skill's Cisco XDR connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact query-incidents parameter description the samples came from -- must match the source query call.",
    upload_ref_help="UUID of a recent query-incidents call (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `cisco_xdr_incidents.py query-incidents ... --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /incidents/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query-incidents call.\n\nSource is either `--ref UUID` (the cache from a recent query call) or\n`--from-file FILE`. Exactly one is required. Both enforce a read-before-write\ncheck: the agent must have queried these exact rows with this exact query\ndescription, the export cannot exceed the upload limit, and a content hash\nmust match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
