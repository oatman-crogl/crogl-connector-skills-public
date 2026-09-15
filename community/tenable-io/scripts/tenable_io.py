#!/usr/bin/env python3
"""tenable-io CLI -- drives a Tenable Vulnerability Management (Tenable.io)
custom connector via the MCP api_proxy tool.

Subcommands:
  query           Filtered search over vulnerability findings, aggregated by
                  plugin (GET /workbenches/vulnerabilities). Emits grouped
                  PSV with a ref for drill-down.
  get             Hydrate one plugin's per-asset finding detail
                  (GET /workbenches/vulnerabilities/{plugin_id}/outputs).
  view-result     Re-print one row (full entity JSON) from a prior query/get.
  refresh, ls, grep, upload-samples   Standard KG catalog/sample commands.

The configured connector's base URL is the Tenable.io API root
(https://cloud.tenable.com); the `X-ApiKeys` header (accessKey + secretKey)
is injected server-side from the connector's stored credentials -- this
package never receives or embeds upstream credentials. This skill is bound
to one configured connector and resolves it automatically; you don't pass a
connector name.
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

SKILL_NAME = "tenable-io"
PACKAGE = "tenable_io"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "tenable_io.py view-result"

# Tenable's `workbenches` endpoints return everything matching the filter in
# a single response -- no cursor/offset pagination is documented for them --
# capped upstream at 5,000 rows and 450 days of data regardless of --limit.
# https://developer.tenable.com/reference/workbenches-vulnerabilities
UPSTREAM_HARD_CAP = 5000
DEFAULT_LIMIT = 25
HARD_CAP = 100
DEFAULT_DATE_RANGE = 30
MAX_FILTERS = 10  # Tenable's documented "no more than 10 filters" limit.

VULNERABILITIES_PATH = "/workbenches/vulnerabilities"
RESOURCES: dict[str, dict[str, str]] = {
    "vulnerabilities": {"list_path": VULNERABILITIES_PATH},
}
RESOURCE_NAMES = tuple(RESOURCES.keys())


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _filter_query(
    filters: tuple[tuple[str, str, str], ...], search_type: str
) -> dict[str, str]:
    """Build the filter.<N>.filter / .quality / .value query params Tenable's
    workbenches endpoints expect from repeatable (field, operator, value)
    triples. https://developer.tenable.com/docs/workbench-filters"""
    if len(filters) > MAX_FILTERS:
        raise click.BadParameter(
            f"at most {MAX_FILTERS} --filter triples are allowed (Tenable's own limit)"
        )
    query: dict[str, str] = {}
    for i, (field, op, value) in enumerate(filters):
        query[f"filter.{i}.filter"] = field
        query[f"filter.{i}.quality"] = op
        query[f"filter.{i}.value"] = value
    if len(filters) > 1:
        query["filter.search_type"] = search_type
    return query


def _dispatch(
    client: mcp.MCPClient,
    connector: str,
    *,
    method: str,
    path: str,
    query: dict[str, str],
    verb: str,
    candidates_path: list[str] | None,
) -> Any:
    r = proxy.dispatch(
        client,
        connector,
        method=method,
        path=path,
        query=query,
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=json.dumps(query, sort_keys=True),
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    try:
        return json.loads(r.body)
    except ValueError as exc:
        raise click.ClickException(f"{verb}: non-JSON response: {r.body[:500]}") from exc


def _search_vulnerabilities(
    client: mcp.MCPClient,
    connector: str,
    query: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    payload = _dispatch(
        client,
        connector,
        method="GET",
        path=VULNERABILITIES_PATH,
        query=query,
        verb="query vulnerabilities",
        candidates_path=["vulnerabilities"],
    )
    rows = payload.get("vulnerabilities") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise click.ClickException(
            f"query vulnerabilities: unexpected response shape: {json.dumps(payload)[:500]}"
        )
    return [row for row in rows if isinstance(row, dict)][:limit]


def _flatten_plugin_outputs(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Flatten the nested outputs -> states -> results -> assets tree
    GET /workbenches/vulnerabilities/{plugin_id}/outputs returns into one row
    per (output, state, result, asset) occurrence, matching the sample shape
    documented at
    https://developer.tenable.com/reference/workbenches-vulnerability-output
    -- this exact response shape has not been live-verified against a real
    tenant; see SKILL.md."""
    outputs = payload.get("outputs") if isinstance(payload, dict) else None
    if not isinstance(outputs, list):
        return []
    rows: list[dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        plugin_output = output.get("plugin_output")
        for state in output.get("states") or []:
            if not isinstance(state, dict):
                continue
            state_name = state.get("name")
            for result in state.get("results") or []:
                if not isinstance(result, dict):
                    continue
                base = {
                    "plugin_output": plugin_output,
                    "state": state_name,
                    "port": result.get("port"),
                    "transport_protocol": result.get("transport_protocol"),
                    "application_protocol": result.get("application_protocol"),
                    "severity": result.get("severity"),
                }
                for asset in result.get("assets") or [{}]:
                    row = dict(base)
                    if isinstance(asset, dict):
                        row["asset_uuid"] = asset.get("uuid")
                        row["ipv4"] = asset.get("ipv4")
                        row["fqdn"] = asset.get("fqdn")
                        row["last_seen"] = asset.get("last_seen")
                    rows.append(row)
    return rows[:limit]


class _TenableSchemaProvider:
    """SchemaProvider for Tenable.io: resource -> field.

    The workbenches API has no createmeta-style schema endpoint, so the leaf
    field tier is discovered empirically: a one-row unfiltered sample of the
    resource, whose top-level JSON keys are the catalog fields.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Tenable Vulnerability Management"
    TIERS_SINGULAR: tuple[str, ...] = ("Resource", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Resources", "Fields")
    # Fields come from a single sampled record (incomplete view), so the
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
            return list(RESOURCE_NAMES)
        if len(tiers) == 1:
            return self._discover_fields(connector, tiers[0])
        return []

    def _discover_fields(self, connector: str, resource: str) -> list[str]:
        if resource not in RESOURCES:
            return []
        rows = _search_vulnerabilities(self._client, connector, {}, 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


_EXAMPLES = """
Examples:

\b
  tenable_io.py query --filter severity eq Critical --limit 25
  tenable_io.py query --filter plugin.name match OpenSSL --date-range 90 --limit 25
  tenable_io.py get --plugin-id 51192 --limit 25
  tenable_io.py view-result --ref <ref> --row 1
  tenable_io.py grep --pattern openssl
  tenable_io.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Query Tenable Vulnerability Management via a configured connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


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
    """Marshal rows to grouped PSV, cache them under a fresh ref for
    drill-down, optionally write a verifiable upload-samples export, and
    print."""
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


@cli.command(name="query")
@click.option(
    "--filter",
    "filters",
    nargs=3,
    multiple=True,
    metavar="FIELD OP VALUE",
    help='Repeatable (field, operator, value) filter triple, e.g. '
    '--filter severity eq Critical. Up to 10. See SKILL.md "Filters".',
)
@click.option(
    "--search-type",
    type=click.Choice(["and", "or"]),
    default="and",
    show_default=True,
    help="Logical operator joining multiple --filter triples.",
)
@click.option(
    "--date-range",
    type=int,
    default=DEFAULT_DATE_RANGE,
    show_default=True,
    help="Days of data prior to and including today to search (Tenable's date_range param; data older than 450 days is never returned).",
)
@click.option(
    "--limit",
    required=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max vulnerabilities to fetch and render (upstream itself caps at {UPSTREAM_HARD_CAP} regardless).",
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
    filters: tuple[tuple[str, str, str], ...],
    search_type: str,
    date_range: int,
    limit: int,
    output_path: str | None,
) -> None:
    """Search vulnerability findings, aggregated by plugin."""
    started = time.monotonic()
    query = _filter_query(filters, search_type)
    query["date_range"] = str(date_range)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_vulnerabilities(client, connector, query, limit)
    _emit_rows(
        ctx,
        rows,
        command="query",
        connector=connector,
        query=json.dumps(query, sort_keys=True),
        catalog_path=["vulnerabilities"],
        started=started,
        output_path=output_path,
    )


@cli.command(name="get")
@click.option(
    "--plugin-id",
    required=True,
    type=int,
    help="Plugin ID from a prior query row's plugin_id field.",
)
@click.option(
    "--date-range",
    type=int,
    default=DEFAULT_DATE_RANGE,
    show_default=True,
    help="Days of data prior to and including today to search.",
)
@click.option(
    "--limit",
    required=True,
    type=click.IntRange(1, HARD_CAP),
    help="Max per-asset occurrence rows to render.",
)
@click.pass_context
def get(ctx: click.Context, plugin_id: int, date_range: int, limit: int) -> None:
    """Hydrate one plugin's per-asset finding detail. Always a fresh backend
    call -- not a cache read of a prior `query` row."""
    started = time.monotonic()
    query = {"date_range": str(date_range)}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _dispatch(
            client,
            connector,
            method="GET",
            path=f"{VULNERABILITIES_PATH}/{plugin_id}/outputs",
            query=query,
            verb="get plugin outputs",
            candidates_path=["vulnerabilities"],
        )
    rows = _flatten_plugin_outputs(payload, limit)
    _emit_rows(
        ctx,
        rows,
        command="get",
        connector=connector,
        query=f"plugin_id={plugin_id}",
        catalog_path=["vulnerabilities"],
        started=started,
        output_path=None,
    )


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query/get.")
@click.option(
    "--result-set",
    default=0,
    show_default=True,
    type=int,
    help="Result-set index (from the PSV footer).",
)
@click.option("--row", required=True, type=int, help="Row number within the result set.")
@click.pass_context
def view_result(ctx: click.Context, ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query/get. Pure cache
    read -- no second backend call."""
    try:
        entity = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(entity, indent=2))


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over the vulnerabilities resource, in the canonical
    row shape upload-samples upserts: plugin-aggregated rows from
    `_search_vulnerabilities` (the same fetch `query` uses, no transform), so
    sampled columns line up with the KG field nodes. A path that is not the
    single known resource returns []."""
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    if column is not None:
        if not values:
            return []
        query = _filter_query(tuple((column, "eq", v) for v in values), "or")
    else:
        query = {}
    return _search_vulnerabilities(client, connector, query, limit)


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _TenableSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Tenable Vulnerability Management",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Tenable.io connectors this skill owns; ``/<connector>/vulnerabilities/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Tenable.io connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill into the field tier below. To search the catalog by name instead of walking it tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a one-row unfiltered sample of the vulnerabilities resource.",
    refresh_docstring="Walk each configured Tenable.io connector's catalog (vulnerabilities -> field) and commit it to the KG.\n\nThe only ``tenable_io.py`` command that hits the connector backend for schema discovery; ``ls`` reads exclusively from the KG cache. Resumable per-tier; stale schema items are pruned at the tier they disappear from.",
    grep_docstring="String-search this skill's Tenable.io connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its LLM-assigned summary, and its tags. Reads the persisted knowledge graph (populated by `refresh` + the kg-learner describe pass), not the live API. Default match is a literal case-insensitive substring; use --regex for a POSIX regex or --fuzzy for typo-tolerant trigram matching. To search every connector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --filter/--date-range combination the samples came from, as JSON (must match the source query; pass '{}' if the query used no filter).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `tenable_io.py query --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /vulnerabilities/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or `--from-file FILE` (a file produced by `query --output`). Exactly one is required. Both enforce a read-before-write check: the agent must have queried these exact rows with this exact `--query`, the export cannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP tool; the server resolves --catalog-path tier-by-tier (lazily creating catalog entries) and walks each sample's nested JSON keys into child nodes under the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
