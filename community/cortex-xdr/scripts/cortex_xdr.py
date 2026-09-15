#!/usr/bin/env python3
"""cortex_xdr CLI -- drives a Palo Alto Cortex XDR custom connector via the
MCP api_proxy tool.

Read-only scope only:
  * Alerts     -- POST /public_api/v1/alerts/get_alerts
  * Incidents  -- POST /public_api/v1/incidents/get_incidents
  * Incident extra data -- POST /public_api/v1/incidents/get_incident_extra_data
  * Endpoints  -- POST /public_api/v1/endpoints/get_endpoints
                  POST /public_api/v1/endpoints/get_endpoint

The configured connector's base URL is ``https://<api_host>``; the stored
Standard API key is injected server-side as ``Authorization`` and the key id
as ``x-xdr-auth-id`` on every call. This skill is bound to one configured
connector and resolves it automatically; you don't pass a connector name.

This connector is deliberately read-only. It does not expose response actions,
incident mutation, or endpoint actions.
"""

from __future__ import annotations

import json
import re
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

SKILL_NAME = "cortex-xdr"
PACKAGE = "cortex-xdr"
PLATFORM_LABEL = "Cortex XDR"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "cortex_xdr.py view-result"
DEFAULT_LIMIT = 25
HARD_CAP = 100

ALERTS_PATH = "/public_api/v1/alerts/get_alerts"
INCIDENTS_PATH = "/public_api/v1/incidents/get_incidents"
INCIDENT_EXTRA_PATH = "/public_api/v1/incidents/get_incident_extra_data"
ENDPOINTS_LIST_PATH = "/public_api/v1/endpoints/get_endpoints"
ENDPOINTS_QUERY_PATH = "/public_api/v1/endpoints/get_endpoint"

RESOURCE_NAMES = ("alerts", "incidents", "endpoints")

ALERT_FILTER_FIELDS = {
    "alert_id_list",
    "alert_source",
    "severity",
    "creation_time",
    "server_creation_time",
}
ALERT_FILTER_OPERATORS = {"in", "gte", "lte"}
ALERT_SORT_FIELDS = {"creation_time", "severity"}

INCIDENT_FILTER_FIELDS = {
    "modification_time",
    "creation_time",
    "incident_id",
    "incident_id_list",
    "description",
    "alert_sources",
    "status",
    "starred",
}
INCIDENT_FILTER_OPERATORS = {"in", "contains", "gte", "lte", "eq", "neq"}
INCIDENT_SORT_FIELDS = {"creation_time", "incident_id", "modification_time"}

ENDPOINT_FILTER_FIELDS = {
    "endpoint_id_list",
    "endpoint_status",
    "dist_name",
    "first_seen",
    "last_seen",
    "ip_list",
    "group_name",
    "platform",
    "alias",
    "isolate",
    "hostname",
    "public_ip_list",
    "cloud_provider",
    "cloud_region",
    "cloud_provider_account_id",
    "cloud_instance_id",
    "cloud_id",
}
ENDPOINT_FILTER_OPERATORS = {"in", "gte", "lte", "eq"}
ENDPOINT_SORT_FIELDS = {"endpoint_id", "first_seen", "last_seen"}


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


def _dispatch_json(
    client: mcp.MCPClient,
    connector: str,
    *,
    method: str,
    path: str,
    verb: str,
    query_desc: str,
    body_obj: dict[str, Any] | None,
    candidates_path: list[str] | None,
) -> Any:
    r = proxy.dispatch(
        client,
        connector,
        method=method,
        path=path,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        body=json.dumps(body_obj or {}),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="rest-key",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_desc,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    return _parse_json(r.body, verb)


def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_filter_value(raw: str, operator: str) -> Any:
    text = raw.strip()
    if operator == "in" and "," in text and not text.startswith("["):
        return [_parse_filter_value(part, "eq") for part in _split_csv(text)]
    try:
        return json.loads(text)
    except ValueError:
        pass
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    if text.lower() == "null":
        return None
    return text


def _build_filters(
    filter_specs: tuple[tuple[str, str, str], ...],
    *,
    allowed_fields: set[str],
    allowed_operators: set[str],
) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for field, operator, value_text in filter_specs:
        if field not in allowed_fields:
            raise click.BadParameter(
                f"unsupported filter field {field!r}; allowed: {', '.join(sorted(allowed_fields))}",
                param_hint="--filter",
            )
        if operator not in allowed_operators:
            raise click.BadParameter(
                f"unsupported filter operator {operator!r}; allowed: {', '.join(sorted(allowed_operators))}",
                param_hint="--filter",
            )
        filters.append(
            {
                "field": field,
                "operator": operator,
                "value": _parse_filter_value(value_text, operator),
            }
        )
    return filters


def _window_end(offset: int, limit: int) -> int:
    """Return a conservative `search_to` bound.

    Vendor docs describe `search_from`/`search_to` as a zero-based range but do
    not make the upper-bound inclusivity perfectly explicit, so callers still
    slice the returned rows client-side to `limit` after this request.
    """
    return offset + limit


def _query_desc(resource: str, **kwargs: Any) -> str:
    return json.dumps({"resource": resource, **kwargs}, sort_keys=True, separators=(",", ":"))


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


def _extract_reply_object(payload: Any, verb: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise click.ClickException(f"{verb}: expected a JSON object response")
    reply = payload.get("reply")
    if not isinstance(reply, dict):
        raise click.ClickException(f"{verb}: expected top-level 'reply' object")
    return reply


def _extract_reply_list(payload: Any, verb: str) -> list[Any]:
    if not isinstance(payload, dict):
        raise click.ClickException(f"{verb}: expected a JSON object response")
    reply = payload.get("reply")
    if not isinstance(reply, list):
        raise click.ClickException(f"{verb}: expected top-level 'reply' array")
    return reply


def _search_alerts(
    client: mcp.MCPClient,
    connector: str,
    *,
    filters: list[dict[str, Any]],
    offset: int,
    limit: int,
    sort_field: str,
    sort_dir: str,
) -> list[dict[str, Any]]:
    request_data: dict[str, Any] = {
        "search_from": offset,
        "search_to": _window_end(offset, limit),
        "sort": {"field": sort_field, "keyword": sort_dir},
    }
    if filters:
        request_data["filters"] = filters
    query_desc = _query_desc(
        "alerts",
        filters=filters,
        offset=offset,
        limit=limit,
        sort={"field": sort_field, "keyword": sort_dir},
    )
    payload = _dispatch_json(
        client,
        connector,
        method="POST",
        path=ALERTS_PATH,
        verb="query alerts",
        query_desc=query_desc,
        body_obj={"request_data": request_data},
        candidates_path=["alerts"],
    )
    reply = _extract_reply_object(payload, "query alerts")
    alerts = reply.get("alerts")
    if not isinstance(alerts, list):
        raise click.ClickException("query alerts: expected reply.alerts array")
    return [row for row in alerts if isinstance(row, dict)][:limit]


def _search_incidents(
    client: mcp.MCPClient,
    connector: str,
    *,
    filters: list[dict[str, Any]],
    offset: int,
    limit: int,
    sort_field: str,
    sort_dir: str,
) -> list[dict[str, Any]]:
    request_data: dict[str, Any] = {
        "search_from": offset,
        "search_to": _window_end(offset, limit),
        "sort": {"field": sort_field, "keyword": sort_dir},
    }
    if filters:
        request_data["filters"] = filters
    query_desc = _query_desc(
        "incidents",
        filters=filters,
        offset=offset,
        limit=limit,
        sort={"field": sort_field, "keyword": sort_dir},
    )
    payload = _dispatch_json(
        client,
        connector,
        method="POST",
        path=INCIDENTS_PATH,
        verb="query incidents",
        query_desc=query_desc,
        body_obj={"request_data": request_data},
        candidates_path=["incidents"],
    )
    reply = _extract_reply_object(payload, "query incidents")
    incidents = reply.get("incidents")
    if not isinstance(incidents, list):
        raise click.ClickException("query incidents: expected reply.incidents array")
    return [row for row in incidents if isinstance(row, dict)][:limit]


def _list_endpoints(
    client: mcp.MCPClient,
    connector: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch the unfiltered endpoint inventory (`get_endpoints`, plural).

    CONTRIBUTING.md rule 4.4 ("every list-returning request must send a
    server-side limiting param") cannot be satisfied here -- re-verified
    2026-09-10 against a public open-source Cortex XDR client
    implementation (ebarti/cortex-xdr-client): `get_endpoints` genuinely
    takes no request body at all (no filters, no search_from/search_to,
    no limit of any kind), unlike `get_endpoint` (singular, used by
    `query-endpoints`/`_search_endpoints` below), which does support both.
    A real large tenant has been publicly reported returning ~7,500 rows
    from this exact call with no way to narrow it. --limit is still
    code-enforced via click.IntRange and applied client-side after the
    fetch (see the slice below) -- the only lever available when the
    vendor exposes none; prefer `query-endpoints` for anything beyond a
    quick, small check on a tenant with a large device fleet.
    """
    query_desc = _query_desc("endpoints-list", limit=limit)
    payload = _dispatch_json(
        client,
        connector,
        method="POST",
        path=ENDPOINTS_LIST_PATH,
        verb="list endpoints",
        query_desc=query_desc,
        body_obj={},
        candidates_path=["endpoints"],
    )
    reply = _extract_reply_list(payload, "list endpoints")
    return [row for row in reply if isinstance(row, dict)][:limit]


def _search_endpoints(
    client: mcp.MCPClient,
    connector: str,
    *,
    filters: list[dict[str, Any]],
    offset: int,
    limit: int,
    sort_field: str,
    sort_dir: str,
) -> list[dict[str, Any]]:
    request_data: dict[str, Any] = {
        "search_from": offset,
        "search_to": _window_end(offset, limit),
        "sort": {"field": sort_field, "keyword": sort_dir},
    }
    if filters:
        request_data["filters"] = filters
    query_desc = _query_desc(
        "endpoints",
        filters=filters,
        offset=offset,
        limit=limit,
        sort={"field": sort_field, "keyword": sort_dir},
    )
    payload = _dispatch_json(
        client,
        connector,
        method="POST",
        path=ENDPOINTS_QUERY_PATH,
        verb="query endpoints",
        query_desc=query_desc,
        body_obj={"request_data": request_data},
        candidates_path=["endpoints"],
    )
    reply = _extract_reply_object(payload, "query endpoints")
    endpoints = reply.get("endpoints")
    if not isinstance(endpoints, list):
        raise click.ClickException("query endpoints: expected reply.endpoints array")
    return [row for row in endpoints if isinstance(row, dict)][:limit]


def _discover_resource_fields(
    client: mcp.MCPClient,
    connector: str,
    resource: str,
) -> list[str]:
    if resource == "alerts":
        rows = _search_alerts(
            client,
            connector,
            filters=[],
            offset=0,
            limit=1,
            sort_field="creation_time",
            sort_dir="desc",
        )
    elif resource == "incidents":
        rows = _search_incidents(
            client,
            connector,
            filters=[],
            offset=0,
            limit=1,
            sort_field="creation_time",
            sort_dir="desc",
        )
    elif resource == "endpoints":
        rows = _search_endpoints(
            client,
            connector,
            filters=[],
            offset=0,
            limit=1,
            sort_field="last_seen",
            sort_dir="DESC",
        )
    else:
        return []
    if not rows:
        return []
    return sorted(str(key) for key in rows[0].keys())


class _CortexXdrSchemaProvider:
    """SchemaProvider for Cortex XDR: resource -> field.

    Cortex XDR's public docs reviewed for this build do not expose a dedicated
    schema endpoint for these resources, so field names are discovered from a
    one-row sample of each resource. This is an incomplete view, so refresh
    unions sampled field names rather than treating them as authoritative.
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
        if len(tiers) == 1:
            return _discover_resource_fields(self._client, connector, tiers[0])
        return []


_EXAMPLES = """
Examples:

\b
  cortex_xdr.py query-alerts --filter severity in high,critical --limit 25
  cortex_xdr.py query-alerts --filter creation_time gte 1720000000000 --limit 10
  cortex_xdr.py query-incidents --filter status eq under_investigation --limit 25
  cortex_xdr.py get-incident-extra-data --id 123456 --alerts-limit 100
  cortex_xdr.py list-endpoints --limit 25
  cortex_xdr.py query-endpoints --filter hostname eq workstation-42 --limit 10
  cortex_xdr.py view-result --ref <ref> --row 1
  cortex_xdr.py grep --pattern severity
  cortex_xdr.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
def cli() -> None:
    """Drive a Palo Alto Cortex XDR connector. Run `<command> --help` for details."""


@cli.command(name="query-alerts")
@click.option(
    "--filter",
    "filter_specs",
    multiple=True,
    nargs=3,
    metavar="FIELD OP VALUE",
    help="Repeatable alert filter triplet. Example: --filter severity in high,critical",
)
@click.option("--offset", default=0, show_default=True, type=click.IntRange(0), help="Zero-based starting offset.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option(
    "--sort-field",
    default="creation_time",
    show_default=True,
    type=click.Choice(sorted(ALERT_SORT_FIELDS)),
    help="Alert sort field.",
)
@click.option(
    "--sort-dir",
    default="desc",
    show_default=True,
    type=click.Choice(["asc", "desc"]),
    help="Alert sort direction.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def query_alerts(
    filter_specs: tuple[tuple[str, str, str], ...],
    offset: int,
    limit: int,
    sort_field: str,
    sort_dir: str,
    output_path: str | None,
) -> None:
    """Query alerts and emit grouped PSV with a ref UUID for drill-down."""
    filters = _build_filters(
        filter_specs,
        allowed_fields=ALERT_FILTER_FIELDS,
        allowed_operators=ALERT_FILTER_OPERATORS,
    )
    query_desc = _query_desc(
        "alerts",
        filters=filters,
        offset=offset,
        limit=limit,
        sort={"field": sort_field, "keyword": sort_dir},
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_alerts(
            client,
            connector,
            filters=filters,
            offset=offset,
            limit=limit,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
    ref = _emit_rows(
        rows,
        command="query-alerts",
        connector=connector,
        query_desc=query_desc,
        catalog_path=["alerts"],
    )
    if output_path and rows and ref is not None:
        samples.write_export(
            output_path,
            producer=f"{SKILL_NAME}/query-alerts",
            connector=connector,
            query=query_desc,
            ref=ref,
            rows=rows,
        )


@cli.command(name="query-incidents")
@click.option(
    "--filter",
    "filter_specs",
    multiple=True,
    nargs=3,
    metavar="FIELD OP VALUE",
    help="Repeatable incident filter triplet. Example: --filter status eq under_investigation",
)
@click.option("--offset", default=0, show_default=True, type=click.IntRange(0), help="Zero-based starting offset.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option(
    "--sort-field",
    default="creation_time",
    show_default=True,
    type=click.Choice(sorted(INCIDENT_SORT_FIELDS)),
    help="Incident sort field.",
)
@click.option(
    "--sort-dir",
    default="desc",
    show_default=True,
    type=click.Choice(["asc", "desc"]),
    help="Incident sort direction.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def query_incidents(
    filter_specs: tuple[tuple[str, str, str], ...],
    offset: int,
    limit: int,
    sort_field: str,
    sort_dir: str,
    output_path: str | None,
) -> None:
    """Query incidents and emit grouped PSV with a ref UUID for drill-down."""
    filters = _build_filters(
        filter_specs,
        allowed_fields=INCIDENT_FILTER_FIELDS,
        allowed_operators=INCIDENT_FILTER_OPERATORS,
    )
    query_desc = _query_desc(
        "incidents",
        filters=filters,
        offset=offset,
        limit=limit,
        sort={"field": sort_field, "keyword": sort_dir},
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_incidents(
            client,
            connector,
            filters=filters,
            offset=offset,
            limit=limit,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
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


@cli.command(name="get-incident-extra-data")
@click.option("--id", "incident_id", required=True, help="Incident id, e.g. 123456.")
@click.option(
    "--alerts-limit",
    default=HARD_CAP,
    show_default=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max related alerts to request for this incident (hard cap {HARD_CAP} -- "
    "the vendor's own default is 1000 with no documented maximum, but this "
    "connector enforces the same ceiling as its other list/query commands).",
)
def get_incident_extra_data(incident_id: str, alerts_limit: int) -> None:
    """Hydrate one incident's related alerts and artifacts as full JSON."""
    query_desc = _query_desc("incident-extra-data", incident_id=incident_id, alerts_limit=alerts_limit)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _dispatch_json(
            client,
            connector,
            method="POST",
            path=INCIDENT_EXTRA_PATH,
            verb="get incident extra data",
            query_desc=query_desc,
            body_obj={"request_data": {"incident_id": incident_id, "alerts_limit": alerts_limit}},
            candidates_path=None,
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="list-endpoints")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows to keep after the unfiltered endpoint list call (hard cap {HARD_CAP}).")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def list_endpoints(limit: int, output_path: str | None) -> None:
    """Fetch the unfiltered endpoint inventory and emit grouped PSV."""
    query_desc = _query_desc("endpoints-list", limit=limit)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _list_endpoints(client, connector, limit=limit)
    ref = _emit_rows(
        rows,
        command="list-endpoints",
        connector=connector,
        query_desc=query_desc,
        catalog_path=["endpoints"],
    )
    if output_path and rows and ref is not None:
        samples.write_export(
            output_path,
            producer=f"{SKILL_NAME}/list-endpoints",
            connector=connector,
            query=query_desc,
            ref=ref,
            rows=rows,
        )


@cli.command(name="query-endpoints")
@click.option(
    "--filter",
    "filter_specs",
    multiple=True,
    nargs=3,
    metavar="FIELD OP VALUE",
    help="Repeatable endpoint filter triplet. Example: --filter hostname eq workstation-42",
)
@click.option("--offset", default=0, show_default=True, type=click.IntRange(0), help="Zero-based starting offset.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option(
    "--sort-field",
    default="last_seen",
    show_default=True,
    type=click.Choice(sorted(ENDPOINT_SORT_FIELDS)),
    help="Endpoint sort field.",
)
@click.option(
    "--sort-dir",
    default="DESC",
    show_default=True,
    type=click.Choice(["ASC", "DESC"]),
    help="Endpoint sort direction (vendor docs show uppercase values).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def query_endpoints(
    filter_specs: tuple[tuple[str, str, str], ...],
    offset: int,
    limit: int,
    sort_field: str,
    sort_dir: str,
    output_path: str | None,
) -> None:
    """Query endpoints and emit grouped PSV with a ref UUID for drill-down."""
    filters = _build_filters(
        filter_specs,
        allowed_fields=ENDPOINT_FILTER_FIELDS,
        allowed_operators=ENDPOINT_FILTER_OPERATORS,
    )
    query_desc = _query_desc(
        "endpoints",
        filters=filters,
        offset=offset,
        limit=limit,
        sort={"field": sort_field, "keyword": sort_dir},
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_endpoints(
            client,
            connector,
            filters=filters,
            offset=offset,
            limit=limit,
            sort_field=sort_field,
            sort_dir=sort_dir,
        )
    ref = _emit_rows(
        rows,
        command="query-endpoints",
        connector=connector,
        query_desc=query_desc,
        catalog_path=["endpoints"],
    )
    if output_path and rows and ref is not None:
        samples.write_export(
            output_path,
            producer=f"{SKILL_NAME}/query-endpoints",
            connector=connector,
            query=query_desc,
            ref=ref,
            rows=rows,
        )


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query/list call.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="1-based row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query/list call."""
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
    if len(segments) != 1:
        return []
    resource = segments[0]
    filters: list[dict[str, Any]] = []
    if column is not None:
        if not values:
            return []
        if resource == "alerts":
            op = "in"
            filters = [{"field": column, "operator": op, "value": values if len(values) > 1 else values[0]}]
        elif resource == "incidents":
            op = "eq" if len(values) == 1 else "in"
            filters = [{"field": column, "operator": op, "value": values if len(values) > 1 else values[0]}]
        elif resource == "endpoints":
            op = "eq" if len(values) == 1 else "in"
            filters = [{"field": column, "operator": op, "value": values if len(values) > 1 else values[0]}]
        else:
            return []

    if resource == "alerts":
        return _search_alerts(
            client,
            connector,
            filters=filters,
            offset=0,
            limit=limit,
            sort_field="creation_time",
            sort_dir="desc",
        )
    if resource == "incidents":
        return _search_incidents(
            client,
            connector,
            filters=filters,
            offset=0,
            limit=limit,
            sort_field="creation_time",
            sort_dir="desc",
        )
    if resource == "endpoints":
        return _search_endpoints(
            client,
            connector,
            filters=filters,
            offset=0,
            limit=limit,
            sort_field="last_seen",
            sort_dir="DESC",
        )
    return []


samplecmds.add_commands(cli, _sample_rows)

kgcommands.add_commands(
    cli,
    lambda client: _CortexXdrSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label=PLATFORM_LABEL,
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Cortex XDR connectors this skill owns; ``/<connector>/alerts/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Cortex XDR connectors' schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: alerts|incidents|endpoints -> Field. Fields are sampled from one row per resource, not discovered from a dedicated schema endpoint.",
    refresh_docstring="Walk the Cortex XDR resource catalog in real time and commit catalog tiers to the KG.\n\nThe only ``cortex-xdr`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier. Because fields are sampled from one row, refresh\nunions field names rather than treating absence as authoritative deletion.",
    grep_docstring="String-search this skill's Cortex XDR connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact filter/sort/offset description the samples came from -- must match the source query/list call.",
    upload_ref_help="UUID of a recent query/list call (read from the in-container cache).",
    upload_from_file_help="Path to a file written by a `cortex_xdr.py ... --output ...` call.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /alerts/<field>).",
    upload_docstring="Verify-then-upload samples from a recent Cortex XDR query.\n\nSource is either `--ref UUID` (the cache from a recent query/list call) or\n`--from-file FILE`. Exactly one is required. Both enforce a read-before-write\ncheck: the agent must have queried these exact rows with this exact query\ndescription, the export cannot exceed the upload limit, and a content hash\nmust match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
