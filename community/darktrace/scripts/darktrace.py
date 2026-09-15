#!/usr/bin/env python3
"""Darktrace API CLI using Crogl's server-side HMAC auth provider.

The CLI uses the standard proxy.dispatch path. Crogl's darktrace_hmac provider
loads the configured token pair server-side and signs each outbound request.
This package does not receive or embed upstream credentials.
"""

from __future__ import annotations

import json
import time
from typing import Any

import click

from _lib import cache, env, instance, kgcommands, mcp, proxy, queryerror
from _lib.psv import Options, marshal_grouped


class _DarktraceSchemaProvider:
    """Static resource catalog; fields are learned after live queries."""

    PACKAGE = "darktrace"
    PLATFORM = "Darktrace"
    TIERS_SINGULAR = ("Resource", "Field")
    TIERS_PLURAL = ("Resources", "Fields")

    def __init__(self, client: mcp.MCPClient) -> None:
        self._client = client

    def list_my_connectors(self) -> list[str]:
        bound = instance.bound_connector_name(self._client)
        return [bound] if bound is not None else []

    def list_children(self, connector: str, tiers: list[str]) -> list[str]:
        return kgcommands.kg_list_children(self._client, connector, tiers)

    def discover_children(self, connector: str, tiers: list[str]) -> list[str]:
        if len(tiers) == 0:
            return ["model-alerts", "incidents", "devices"]
        return []

SKILL_NAME = "darktrace"
PACKAGE = "darktrace"
VIEW_TOOL_NAME = "darktrace.py view-result"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
DEFAULT_LIMIT = 25
HARD_CAP = 100


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _dispatch(
    client: mcp.MCPClient,
    connector: str,
    *,
    method: str,
    path: str,
    query: dict[str, str] | None,
    body: str | None = None,
) -> Any:
    """Dispatch a request through Crogl's server-side Darktrace signer."""
    r = proxy.dispatch(
        client,
        connector,
        method=method,
        path=path,
        query=query or {},
        headers={"Accept": "application/json"},
        body=body,
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=f"{method} {path}",
            connector=connector,
            package=PACKAGE,
            query=json.dumps(query or {}, sort_keys=True),
            response=r,
            candidates_path=None,
            client=client,
        )
    try:
        return json.loads(r.body)
    except ValueError as exc:
        raise click.ClickException(f"Darktrace returned non-JSON response: {r.body[:500]}") from exc


def _rows(payload: Any, resource: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in (resource, "data", "response", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [payload]
    raise click.ClickException(f"Darktrace {resource}: unexpected JSON response")


def _emit_rows(rows: list[dict[str, Any]], *, command: str, connector: str, query: str, catalog_path: list[str]) -> None:
    ref = f"{time.time_ns():x}"
    if not rows:
        click.echo(f"No {catalog_path[0]} results (0 rows).")
        return
    body, groups = marshal_grouped(
        rows[:HARD_CAP],
        SEARCH_OPTS,
        max_result_sets=MAX_RESULT_SETS,
        ref=ref,
        view_tool_name=VIEW_TOOL_NAME,
    )
    cache.put(groups, ref=ref, producer=f"{SKILL_NAME}/{command}", connector=connector, query=query, catalog_path=catalog_path)
    click.echo(body)


_EXAMPLES = """
Examples:

  darktrace.py query-model-alerts --starttime 1701388800000 --endtime 1704070800000
  darktrace.py query-incidents --starttime 1701388800000 --endtime 1704070800000
  darktrace.py list-devices
  darktrace.py view-result --ref <ref> --row 1
  darktrace_signing.py is tested independently against the supplied vendor vector.
"""


@click.group(epilog=_EXAMPLES)
def cli() -> None:
    """Query Darktrace through a configured connector."""


@cli.command(name="query-model-alerts")
@click.option("--starttime", required=True, help="Start time in Unix milliseconds.")
@click.option("--endtime", required=True, help="End time in Unix milliseconds.")
@click.option("--limit", default=DEFAULT_LIMIT, type=click.IntRange(1, HARD_CAP), show_default=True)
def query_model_alerts(starttime: str, endtime: str, limit: int) -> None:
    """Query /modelbreaches for a bounded time window."""
    _query("model-alerts", "/modelbreaches", {"starttime": starttime, "endtime": endtime}, limit)


@cli.command(name="query-incidents")
@click.option("--starttime", required=True, help="Start time in Unix milliseconds.")
@click.option("--endtime", required=True, help="End time in Unix milliseconds.")
@click.option("--limit", default=DEFAULT_LIMIT, type=click.IntRange(1, HARD_CAP), show_default=True)
def query_incidents(starttime: str, endtime: str, limit: int) -> None:
    """Query /aianalyst/incidentevents for a bounded time window."""
    _query("incidents", "/aianalyst/incidentevents", {"starttime": starttime, "endtime": endtime}, limit)


@cli.command(name="list-devices")
@click.option("--limit", default=DEFAULT_LIMIT, type=click.IntRange(1, HARD_CAP), show_default=True)
def list_devices(limit: int) -> None:
    """List tracked devices from /devices."""
    _query("devices", "/devices", {}, limit)


def _query(resource: str, path: str, query: dict[str, str], limit: int) -> None:
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _dispatch(client, connector, method="GET", path=path, query=query)
    rows = _rows(payload, resource)[:limit]
    _emit_rows(rows, command=resource, connector=connector, query=json.dumps(query, sort_keys=True), catalog_path=[resource])


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference from a prior query.")
@click.option("--row", required=True, type=click.IntRange(1), help="1-based cached row number.")
def view_result(ref: str, row: int) -> None:
    """Re-print a row from a cached result."""
    try:
        click.echo(json.dumps(cache.get_row(ref, 0, row - 1), indent=2))
    except cache.CacheMiss as exc:
        raise click.ClickException(str(exc)) from exc


kgcommands.add_commands(
    cli,
    lambda client: _DarktraceSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Darktrace",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Darktrace connectors this skill owns; deeper paths list resources and learned fields.",
    ls_docstring="List the Darktrace connector schema by UNIX path. The resource catalog is based on the supplied documentation; field discovery remains pending a live signed request.",
    refresh_docstring="Refresh the Darktrace resource catalog. Live field discovery remains unverified until a signed request succeeds against a real instance.",
    grep_docstring="Search the persisted Darktrace knowledge-graph catalog.",
    upload_query_help="The exact query description used to obtain the sample.",
    upload_ref_help="UUID of a recent Darktrace query result.",
    upload_from_file_help="Path to a Darktrace query export.",
    upload_catalog_path_help="POSIX-style catalog path, such as /model-alerts/score.",
    upload_docstring="Verify and upload a sample from a prior Darktrace query.",
)


if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
