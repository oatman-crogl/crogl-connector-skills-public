#!/usr/bin/env python3
"""taegis CLI — drives a Secureworks Taegis XDR (Sophos) custom connector via
the MCP api_proxy tool.

Taegis exposes a single GraphQL gateway at ``POST {base_url}/graphql``. Every
subcommand builds a GraphQL document + variables and dispatches it through the
configured connector; auth (an OAuth2 client-credentials Bearer, minted
server-side from the connector's stored client_id/client_secret) is attached by
crogld — this container never holds the secret.

Subcommands:
  query          Search a resource (alerts|investigations|assets). alerts and
                  investigations take a CQL statement; assets take a JSON
                  AssetFilter. Emits grouped PSV with a ref for drill-down.
  view-result     Drill into one row (full entity JSON) from a prior query.
  get             Hydrate a single entity by id (no ref needed).
  upload-samples  Verify-then-upload sampled entities to the knowledge graph.
  refresh         Walk each connector's catalog (Resource -> Field) into the KG.
  ls              List this skill's Taegis connectors' database schema by path.
  grep            String-search this skill's connectors' KG catalog.

The configured connector's base URL is the Taegis regional API root
(https://api.ctpx.secureworks.com and the other regions listed in SKILL.md).

DRAFT NOTE — the GraphQL operation names, input types, variables, and selection
sets below were cross-checked against the code-generated `taegis-sdk-python`
schema (operations `alertsServiceSearch`/`alertsServiceRetrieveAlertsById`,
`investigationsV2`/`investigationV2`, `assetsV2`). They are schema-accurate but
not yet exercised against a live tenant; each resource's GraphQL is isolated in
the ``RESOURCES`` block so any residual correction is a one-line edit.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

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

SKILL_NAME = "taegis"
PACKAGE = "taegis"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "taegis.py view-result"
GRAPHQL_PATH = "/graphql"
# CQL is Taegis's query language for alerts/investigations; queryerror has no
# per-interface grammar for it, so hints degrade to the raw upstream message.
QUERY_INTERFACE = "cql"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). All three resources share one ceiling
# -- none has a separate bulk-listing command. MAX_PAGE_SIZE is the explicit
# per-page size (offset/perPage/first) sent on every request -- capped at
# HARD_CAP since no query here ever needs more than one page's worth beyond
# that.
DEFAULT_LIMIT = 25
HARD_CAP = 100
MAX_PAGE_SIZE = HARD_CAP


# --- GraphQL documents (doc-derived; verify against live schema) ------------
#
# Each resource declares the two GraphQL documents it needs (search + get) and
# small callables that (a) build the request variables and (b) extract the row
# list, total, and next-page cursor from the response ``data``. Keeping every
# resource's GraphQL text and shape-handling in one place makes a schema
# correction a localized edit rather than a scavenger hunt through the CLI.

_ALERTS_FIELDS = """
        id
        tenant_id
        status
        metadata {
          title
          severity
          confidence
          created_at { seconds nanos }
          description
        }
        attack_technique_ids
        sensor_types
        investigation_ids
"""

_ALERTS_SEARCH = f"""
query alertsServiceSearch($in: SearchRequestInput) {{
  alertsServiceSearch(in: $in) {{
    alerts {{
      total_results
      list {{{_ALERTS_FIELDS}      }}
    }}
  }}
}}
"""

_ALERTS_GET = f"""
query alertsServiceRetrieveAlertsById($in: GetByIDRequestInput) {{
  alertsServiceRetrieveAlertsById(in: $in) {{
    alerts {{
      list {{{_ALERTS_FIELDS}      }}
    }}
  }}
}}
"""

_INVESTIGATIONS_FIELDS = """
      id
      shortId
      title
      priority
      type
      status
      assigneeId
      createdAt
      updatedAt
      keyFindings
      closeReason
"""

_INVESTIGATIONS_SEARCH = f"""
query investigationsV2($arguments: InvestigationsV2Arguments) {{
  investigationsV2(arguments: $arguments) {{
    totalCount
    investigations {{{_INVESTIGATIONS_FIELDS}    }}
  }}
}}
"""

_INVESTIGATIONS_GET = f"""
query investigationV2($arguments: InvestigationV2Arguments) {{
  investigationV2(arguments: $arguments) {{{_INVESTIGATIONS_FIELDS}  }}
}}
"""

_ASSETS_FIELDS = """
      id
      hostId
      hostnames { hostname }
      ipAddresses { ip }
      osFamily
      osVersion
      architecture
      systemType
      endpointType
      endpointPlatform
      sensorVersion
      status
      tags { tag }
      isolationStatus
      lastSeenAt
      createdAt
      updatedAt
"""

_ASSETS_SEARCH = f"""
query assetsV2($first: Int, $after: String, $filter: AssetFilter, $orderBy: AssetSearchOrderByInputV2) {{
  assetsV2(first: $first, after: $after, filter: $filter, orderBy: $orderBy) {{
    totalCount
    assets {{{_ASSETS_FIELDS}    }}
    pageInfo {{
      endCursor
      hasNextPage
    }}
  }}
}}
"""


@dataclass(frozen=True)
class _Resource:
    """One Taegis GraphQL surface and how to page it.

    ``pagination`` is ``"offset"`` (alerts: an integer offset into a flat result
    set), ``"page"`` (investigations: a 1-based page number with a per-page
    size), or ``"cursor"`` (assets: an opaque Relay-style cursor).
    ``search_doc``/``get_doc`` are the GraphQL documents; the extractor callables
    pull rows / total / next-cursor out of the response ``data`` dict.
    """

    name: str
    pagination: str
    search_doc: str
    get_doc: str
    # (filter_str, limit, cursor) -> variables dict for the search document.
    search_vars: Callable[[str, int, Any], dict[str, Any]]
    # data -> (rows, total_or_None, next_cursor_or_None)
    search_extract: Callable[[dict[str, Any]], tuple[list[dict[str, Any]], int | None, Any]]
    # entity_id -> variables dict for the get document.
    get_vars: Callable[[str], dict[str, Any]]
    # data -> the single entity dict (or None).
    get_extract: Callable[[dict[str, Any]], dict[str, Any] | None]
    # Human default filter shown when --filter is omitted.
    default_filter: str


def _dig(data: dict[str, Any], *path: str) -> Any:
    """Walk nested dict keys, returning None if any hop is missing/not a dict."""
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _alerts_extract(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None, Any]:
    node = _dig(data, "alertsServiceSearch", "alerts")
    rows = node.get("list") if isinstance(node, dict) else None
    total = node.get("total_results") if isinstance(node, dict) else None
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return rows, (int(total) if isinstance(total, int) else None), None


def _investigations_extract(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None, Any]:
    node = _dig(data, "investigationsV2")
    rows = node.get("investigations") if isinstance(node, dict) else None
    total = node.get("totalCount") if isinstance(node, dict) else None
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    return rows, (int(total) if isinstance(total, int) else None), None


def _assets_extract(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None, Any]:
    node = _dig(data, "assetsV2")
    rows = node.get("assets") if isinstance(node, dict) else None
    total = node.get("totalCount") if isinstance(node, dict) else None
    page = node.get("pageInfo") if isinstance(node, dict) else None
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    next_cursor = None
    if isinstance(page, dict) and page.get("hasNextPage"):
        next_cursor = page.get("endCursor")
    return rows, (int(total) if isinstance(total, int) else None), next_cursor


def _assets_filter_var(filter_str: str) -> dict[str, Any] | None:
    """Parse the assets --filter (a JSON AssetFilter) into a variable value."""
    filter_str = filter_str.strip()
    if not filter_str:
        return None
    try:
        parsed = json.loads(filter_str)
    except json.JSONDecodeError as e:
        raise click.BadParameter(
            f"assets --filter must be a JSON AssetFilter object: {e}"
        ) from e
    if not isinstance(parsed, dict):
        raise click.BadParameter("assets --filter must be a JSON object")
    return parsed


RESOURCES: dict[str, _Resource] = {
    "alerts": _Resource(
        name="alerts",
        pagination="offset",
        search_doc=_ALERTS_SEARCH,
        get_doc=_ALERTS_GET,
        search_vars=lambda f, limit, cursor: {
            "in": {"cql_query": f, "limit": limit, "offset": int(cursor or 0)}
        },
        search_extract=_alerts_extract,
        get_vars=lambda entity_id: {"in": {"iDs": [entity_id]}},
        get_extract=lambda data: next(
            iter(_dig(data, "alertsServiceRetrieveAlertsById", "alerts", "list") or []),
            None,
        ),
        default_filter="FROM alert WHERE severity >= 0.6 EARLIEST=-7d",
    ),
    "investigations": _Resource(
        name="investigations",
        pagination="page",
        search_doc=_INVESTIGATIONS_SEARCH,
        get_doc=_INVESTIGATIONS_GET,
        search_vars=lambda f, limit, cursor: {
            "arguments": {"cql": f, "page": int(cursor or 1), "perPage": limit}
        },
        search_extract=_investigations_extract,
        get_vars=lambda entity_id: {"arguments": {"id": entity_id}},
        get_extract=lambda data: _dig(data, "investigationV2"),
        default_filter="",
    ),
    "assets": _Resource(
        name="assets",
        pagination="cursor",
        search_doc=_ASSETS_SEARCH,
        get_doc=_ASSETS_SEARCH,
        search_vars=lambda f, limit, cursor: {
            "first": limit,
            "after": cursor,
            "filter": _assets_filter_var(f),
            "orderBy": "updated_at_desc",
        },
        search_extract=_assets_extract,
        get_vars=lambda entity_id: {
            "first": 1,
            "after": None,
            "filter": {"where": {"id": entity_id}},
            "orderBy": "updated_at_desc",
        },
        get_extract=lambda data: next(
            iter(_dig(data, "assetsV2", "assets") or []), None
        ),
        default_filter="",
    ),
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


def _graphql(
    client: mcp.MCPClient,
    connector: str,
    resource: str,
    document: str,
    variables: dict[str, Any],
    *,
    verb: str,
    query: str | None,
) -> dict[str, Any]:
    """POST one GraphQL document and return its ``data`` object.

    Two error surfaces, handled distinctly: a non-2xx HTTP status (auth,
    rate-limit, gateway) routes through ``queryerror.actionable`` like the REST
    connectors; a 200 response carrying a GraphQL ``errors`` array (a bad query,
    unknown field, or schema mismatch) raises a plain, message-bearing error —
    GraphQL reports those in-band, not via status code.
    """
    body = json.dumps({"query": document, "variables": variables})
    r = proxy.dispatch(
        client,
        connector,
        method="POST",
        path=GRAPHQL_PATH,
        headers={"Content-Type": "application/json"},
        body=body,
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface=QUERY_INTERFACE,
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query,
            response=r,
            candidates_path=[resource],
            client=client,
        )
    try:
        payload = json.loads(r.body)
    except json.JSONDecodeError as e:
        raise click.ClickException(
            f"{verb}: Taegis GraphQL returned non-JSON body: {e}"
        ) from e
    if isinstance(payload, dict) and payload.get("errors"):
        msgs = "; ".join(
            str(err.get("message", err)) if isinstance(err, dict) else str(err)
            for err in payload["errors"]
        )
        raise click.ClickException(f"{verb}: GraphQL error(s): {msgs}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise click.ClickException(f"{verb}: GraphQL response had no data object")
    return data


def _search(
    client: mcp.MCPClient, connector: str, resource: str, filter_str: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Page a resource up to ``limit`` rows.

    ``offset`` (alerts) advances an integer offset by the page size; ``page``
    (investigations) increments a 1-based page number; ``cursor`` (assets)
    follows ``pageInfo.endCursor`` while ``hasNextPage``. All stop at ``limit``,
    an empty page, or a null next-page token.

    ``limit`` is clamped to HARD_CAP before the loop starts (the CLI's own
    click.IntRange already enforces this for `query`, but this function is
    also reachable directly from the shared `sample`/`lookup` commands,
    whose own --limit is not IntRange-bounded). Each request's page size
    (offset/perPage/first) is likewise capped at MAX_PAGE_SIZE rather than
    asking for the full remaining budget in one page.

    Returns ``(rows, truncated)`` -- ``truncated`` is True when the loop
    stopped because it hit ``limit`` while more rows were still available
    upstream (per the response's total/pageInfo, which the search documents
    always request but the pagination loop itself doesn't need to read --
    surfacing it here is what keeps that data from going dead in the query,
    per CONTRIBUTING.md rule 4.6).
    """
    limit = min(limit, HARD_CAP)
    spec = RESOURCES[resource]
    if not filter_str:
        filter_str = spec.default_filter
    rows: list[dict[str, Any]] = []
    cursor: Any = {"offset": 0, "page": 1, "cursor": None}[spec.pagination]
    last_total: int | None = None
    last_next_cursor: Any = None
    while len(rows) < limit:
        page_limit = min(limit - len(rows), MAX_PAGE_SIZE)
        variables = spec.search_vars(filter_str, page_limit, cursor)
        data = _graphql(
            client,
            connector,
            resource,
            spec.search_doc,
            variables,
            verb=f"{resource} query",
            query=filter_str,
        )
        page, total, next_cursor = spec.search_extract(data)
        last_total, last_next_cursor = total, next_cursor
        if not page:
            break
        rows.extend(page)
        if spec.pagination == "offset":
            cursor = int(cursor or 0) + len(page)
        elif spec.pagination == "page":
            cursor = int(cursor or 1) + 1
        else:
            if not next_cursor:
                break
            cursor = next_cursor
    truncated = len(rows) >= limit and (
        (last_total is not None and last_total > limit)
        or (spec.pagination == "cursor" and bool(last_next_cursor))
    )
    return rows[:limit], truncated


def _get_entity(
    client: mcp.MCPClient, connector: str, resource: str, entity_id: str
) -> dict[str, Any] | None:
    spec = RESOURCES[resource]
    data = _graphql(
        client,
        connector,
        resource,
        spec.get_doc,
        spec.get_vars(entity_id),
        verb=f"get {resource}",
        query=None,
    )
    return spec.get_extract(data)


class _TaegisSchemaProvider:
    """SchemaProvider for Taegis: resource -> field.

    Taegis GraphQL has an introspectable schema, but this skill's catalog only
    needs the field tier as it appears in returned entities, so the leaf tier is
    discovered empirically from a one-row sample of each resource (its top-level
    JSON keys) — the same approach the Defender connector uses. Endpoint-asset
    identity fields learned here (hostnames, ipAddresses) are the join keys that
    tie an alert or investigation back to a device.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Taegis"
    TIERS_SINGULAR: tuple[str, ...] = ("Resource", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Resources", "Fields")
    # Fields come from a single sampled record (incomplete view), so the refresh
    # walk unions rather than prunes them.
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
        rows, _truncated = _search(self._client, connector, resource, "", 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


_EXAMPLES = """
Examples:

\b
  taegis.py query --resource alerts --filter "FROM alert WHERE severity >= 0.6 AND status = 'OPEN' EARLIEST=-1d" --limit 50
  taegis.py query --resource investigations --filter "status = 'OPEN'" --limit 25
  taegis.py query --resource assets --filter '{"assetState": ["Active"]}' --limit 50
  taegis.py get --resource assets --id <asset-id>
  taegis.py view-result --ref <ref> --row 1
  taegis.py grep --pattern hostname
  taegis.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Secureworks Taegis XDR connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(sorted(RESOURCE_NAMES)),
    default="alerts",
    show_default=True,
    help="Taegis resource to query.",
)
@click.option(
    "--filter",
    "filter_str",
    default="",
    help="alerts/investigations: a CQL statement (e.g. \"FROM alert WHERE "
    "severity >= 0.6 EARLIEST=-1d\"). assets: a JSON AssetFilter object (e.g. "
    "'{\"assetState\": [\"Active\"]}'). Empty uses the resource default.",
)
@click.option(
    "--limit",
    required=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max entities to fetch (hard cap {HARD_CAP}). No default -- "
    f"{DEFAULT_LIMIT} is a reasonable starting point for an analyst-facing "
    "question; see SKILL.md.",
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
    resource: str,
    filter_str: str,
    limit: int,
    output_path: str | None,
) -> None:
    """Run a search and emit grouped PSV with a ref UUID for drill-down."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows, truncated = _search(client, connector, resource, filter_str, limit)

    if truncated:
        click.echo(
            f"Note: more {resource} than --limit={limit} are available "
            "upstream; showing the first page only. Raise --limit (up to "
            f"{HARD_CAP}) or narrow --filter to see more.",
            err=True,
        )

    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/query"

    if not rows:
        filter_note = f" matching {filter_str!r}" if filter_str else ""
        click.echo(f"No {resource} results (0 rows){filter_note}.")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            resource=resource,
            filter=filter_str,
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
        query=filter_str,
        catalog_path=[resource],
    )

    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=filter_str,
            ref=ref,
            rows=rows,
        )

    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        resource=resource,
        filter=filter_str,
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
    """Print the full JSON for one entity from a prior query.

    Taegis entities are returned in full at search time (the query's selection
    set), so this is a pure cache read — no second backend call needed.
    """
    try:
        entity = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(entity, indent=2))


@cli.command(name="get")
@click.option(
    "--resource",
    type=click.Choice(sorted(RESOURCE_NAMES)),
    default="alerts",
    show_default=True,
    help="Taegis resource the id belongs to.",
)
@click.option("--id", "entity_id", required=True, help="Entity id to hydrate.")
@click.pass_context
def get(ctx: click.Context, resource: str, entity_id: str) -> None:
    """Hydrate a single entity by id (no prior query ref needed)."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        entity = _get_entity(client, connector, resource, entity_id)
    if entity is None:
        raise click.ClickException(f"no {resource} entity found for id {entity_id!r}")
    click.echo(json.dumps(entity, indent=2))


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a Taegis resource, in the canonical row shape
    upload-samples upserts: entity objects from ``_search`` (the same fetch
    ``query`` uses, no transform), so the sampled columns line up with the KG
    field nodes. A path that is not a single known resource returns [].

    A ``column``/``values`` lookup is only wired for the CQL resources
    (alerts, investigations), where an ``IN`` list maps to CQL ``OR`` terms; a
    column lookup against the structured assets filter is not supported here, so
    it returns [] rather than guessing a filter shape.
    """
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    resource = segments[0]
    spec = RESOURCES[resource]
    filter_str = ""
    if column is not None:
        if not values or spec.pagination != "offset":
            return []
        filter_str = "FROM {r} WHERE {clause}".format(
            r=resource.rstrip("s") if resource == "alerts" else resource,
            clause=" OR ".join(f"{column} = '{v}'" for v in values),
        )
    rows, _truncated = _search(client, connector, resource, filter_str, limit)
    return rows


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _TaegisSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Taegis",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Taegis connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Taegis connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\none-row sample of each resource.",
    refresh_docstring="Walk Taegis alerts/investigations/assets schema in real time and commit catalog tiers to the KG.\n\nThe only ``taegis`` command that hits the connector backend for schema\ndiscovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Taegis connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --filter the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `taegis.py query --output …`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /alerts/<field> or /assets/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (filter), the export\ncannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
