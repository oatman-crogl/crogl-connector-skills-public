#!/usr/bin/env python3
"""SolarWinds Service Desk / Samanage community connector CLI."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import click

from _lib import cache, env, instance, kgcommands, mcp, proxy, queryerror, samples
from _lib.psv import Options, marshal_grouped


SKILL_NAME = "solarwinds-service-desk"
PACKAGE = "solarwinds-service-desk"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "solarwinds_service_desk.py view-result"
CATALOG_RESOURCES = ("incidents", "users", "groups", "categories")


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _api_path(resource: str, obj_id: str | None = None, child: str | None = None) -> str:
    path = f"/{resource}"
    if obj_id:
        path += f"/{obj_id}"
    if child:
        path += f"/{child}"
    return path + ".json"


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.samanage.v2.1+json",
        "Content-Type": "application/json",
    }


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _request(
    client: mcp.MCPClient,
    connector: str,
    *,
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    verb: str,
) -> Any:
    r = proxy.dispatch(
        client,
        connector,
        method=method,
        path=path,
        query=query or {},
        headers=_headers(),
        body=json.dumps(body) if body is not None else None,
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ticketing",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=None,
            client=client,
        )
    return _parse_json(r.body, verb)


def _params(param: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in param:
        if "=" not in item:
            raise click.BadParameter("use key=value", param_hint="--param")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _fields(field: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in field:
        if "=" not in item:
            raise click.BadParameter("use key=value", param_hint="--field")
        k, v = item.split("=", 1)
        out[k] = v
    return out


def _name_or_email(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("email") or value.get("name") or "")


def _summarize_incident(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "number": row.get("number"),
        "name": row.get("name"),
        "state": row.get("state"),
        "priority": row.get("priority"),
        "requester": _name_or_email(row.get("requester")),
        "assignee": _name_or_email(row.get("assignee")),
        "updated_at": row.get("updated_at"),
        "comments": row.get("number_of_comments"),
    }


def _list_incidents(
    ctx: click.Context,
    page: int,
    per_page: int,
    param: tuple[str, ...],
    output_path: str | None,
) -> None:
    started = time.monotonic()
    query = {"page": str(page), "per_page": str(per_page), **_params(param)}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _request(client, connector, method="GET", path=_api_path("incidents"), query=query, verb="search incidents")
    if not isinstance(rows, list):
        raise click.ClickException("search incidents: expected list response")
    display_rows = [_summarize_incident(r) for r in rows if isinstance(r, dict)]
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/search-incidents"
    body_text, groups = marshal_grouped(
        display_rows,
        SEARCH_OPTS,
        max_result_sets=MAX_RESULT_SETS,
        ref=ref,
        view_tool_name=VIEW_TOOL_NAME,
    )
    cache.put(groups, ref=ref, producer=producer, connector=connector, query=json.dumps(query, sort_keys=True))
    if output_path:
        samples.write_export(output_path, producer=producer, connector=connector, query=json.dumps(query, sort_keys=True), ref=ref, rows=rows)
    click.echo(body_text)
    kgcommands.emit_meta(ctx.obj["json_meta"], ref=ref, rows=len(rows), duration_ms=int((time.monotonic() - started) * 1000))


class _SchemaProvider:
    PACKAGE = PACKAGE
    PLATFORM = "SolarWinds Service Desk"
    TIERS_SINGULAR = ("Resource", "Field")
    TIERS_PLURAL = ("Resources", "Fields")
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
            return list(CATALOG_RESOURCES)
        if len(tiers) == 1:
            payload = _request(
                self._client,
                connector,
                method="GET",
                path=_api_path(tiers[0]),
                query={"per_page": "1"},
                verb=f"sample {tiers[0]}",
            )
            rows = payload if isinstance(payload, list) else []
            return sorted(k for k in (rows[0] if rows else {}) if isinstance(k, str))
        return []


@click.group()
@click.option("--json-meta", is_flag=True, help="Print a trailing JSON metadata line.")
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive SolarWinds Service Desk incidents."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="search-incidents")
@click.option("--page", default=1, show_default=True, type=click.IntRange(1))
@click.option("--per-page", default=25, show_default=True, type=click.IntRange(1, 100))
@click.option("--param", multiple=True, help="Extra query parameter as key=value.")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True))
@click.pass_context
def search_incidents(ctx: click.Context, page: int, per_page: int, param: tuple[str, ...], output_path: str | None) -> None:
    """List incidents."""
    _list_incidents(ctx, page, per_page, param, output_path)


@cli.command(name="list-incidents")
@click.option("--page", default=1, show_default=True, type=click.IntRange(1))
@click.option("--per-page", default=25, show_default=True, type=click.IntRange(1, 100))
@click.option("--param", multiple=True, help="Extra query parameter as key=value.")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True))
@click.pass_context
def list_incidents(ctx: click.Context, page: int, per_page: int, param: tuple[str, ...], output_path: str | None) -> None:
    """Alias for search-incidents."""
    _list_incidents(ctx, page, per_page, param, output_path)


@cli.command(name="get-incident")
@click.option("--id", "incident_id", required=True, help="Numeric incident ID.")
@click.option("--layout", type=click.Choice(["short", "long"]), default="short", show_default=True)
def get_incident(incident_id: str, layout: str) -> None:
    """Fetch one incident by ID."""
    query = {"layout": layout} if layout == "long" else {}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        row = _request(client, connector, method="GET", path=_api_path("incidents", incident_id), query=query, verb="get incident")
    click.echo(json.dumps(row, indent=2))


@cli.command(name="create-incident")
@click.option("--name", required=True)
@click.option("--description", default="")
@click.option("--requester-email", default=None)
@click.option("--priority", default=None)
@click.option("--category", default=None)
@click.option("--field", multiple=True, help="Extra incident field as key=value.")
def create_incident(name: str, description: str, requester_email: str | None, priority: str | None, category: str | None, field: tuple[str, ...]) -> None:
    """Create an incident."""
    incident = {"name": name, "description": description, **_fields(field)}
    if requester_email:
        incident["requester"] = {"email": requester_email}
    if priority:
        incident["priority"] = priority
    if category:
        incident["category"] = {"name": category}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        row = _request(client, connector, method="POST", path=_api_path("incidents"), body={"incident": incident}, verb="create incident")
    click.echo(json.dumps(row, indent=2))


@cli.command(name="update-incident")
@click.option("--id", "incident_id", required=True, help="Numeric incident ID.")
@click.option("--name", default=None)
@click.option("--description", default=None)
@click.option("--priority", default=None)
@click.option("--state-id", default=None)
@click.option("--field", multiple=True, help="Extra incident field as key=value.")
def update_incident(incident_id: str, name: str | None, description: str | None, priority: str | None, state_id: str | None, field: tuple[str, ...]) -> None:
    """Update incident fields."""
    incident = _fields(field)
    if name is not None:
        incident["name"] = name
    if description is not None:
        incident["description"] = description
    if priority is not None:
        incident["priority"] = priority
    if state_id is not None:
        incident["state_id"] = state_id
    if not incident:
        raise click.UsageError("provide at least one field to update")
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        row = _request(client, connector, method="PUT", path=_api_path("incidents", incident_id), body={"incident": incident}, verb="update incident")
    click.echo(json.dumps(row, indent=2))


@cli.command(name="add-comment")
@click.option("--id", "incident_id", required=True, help="Numeric incident ID.")
@click.option("--message", required=True, help="Plain-text public comment body.")
@click.option("--private", "is_private", is_flag=True, help="Mark comment private if the API supports it.")
def add_comment(incident_id: str, message: str, is_private: bool) -> None:
    """Best-effort comment write. May be unavailable through API-token auth."""
    body = {"comment": {"body": f"<p>{message}</p>", "is_private": is_private}}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        row = _request(client, connector, method="POST", path=_api_path("incidents", incident_id, "comments"), body=body, verb="add comment")
    click.echo(json.dumps(row, indent=2))


@cli.command(name="view-result")
@click.option("--ref", required=True)
@click.option("--row", type=click.IntRange(1), required=True)
def view_result(ref: str, row: int) -> None:
    """Re-print one row from a prior search."""
    try:
        record = cache.get_row(ref, 0, row - 1)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


kgcommands.add_commands(
    cli,
    lambda client: _SchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="SolarWinds Service Desk",
    ls_path_help="UNIX-style absolute schema path. `/` lists connectors; `/<connector>` lists resources; `/<connector>/<resource>` lists sampled fields.",
    ls_docstring="List this skill's SolarWinds Service Desk schema by UNIX path.",
    refresh_docstring="Sample common SolarWinds Service Desk resources and commit resource/field tiers to the KG.",
    grep_docstring="Search this skill's SolarWinds Service Desk KG catalog.",
    upload_query_help="The exact JSON query metadata emitted by search-incidents.",
    upload_ref_help="UUID from a prior search-incidents run.",
    upload_from_file_help="Path to a file written by search-incidents --output.",
    upload_catalog_path_help="Catalog path, usually /incidents/<field>.",
    upload_docstring="Verify-then-upload rows from a recent search-incidents run.",
    upload_mismatch_msg="query mismatch for search-incidents sample",
    upload_limit_msg=lambda n: f"upload limit is {samples.MAX_UPLOAD_ROWS} rows, this cache holds {n}",
)


if __name__ == "__main__":
    cli()
