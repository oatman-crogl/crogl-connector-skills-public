#!/usr/bin/env python3
"""sophos_central CLI — drives a Sophos Central (Fusion) custom connector via
the MCP api_proxy tool.

Sophos Central is a tenant-scoped REST API. The connector's base URL is the
tenant's data-region host (e.g. https://api-us01.central.sophos.com); crogld
attaches the OAuth2 Bearer (minted server-side from the stored client_id/secret
via https://id.sophos.com/api/v2/oauth2/token) and the required
``X-Tenant-ID`` header (from the stored tenant_id) on every call. This
container never holds the secret, the client id, or the tenant id.

Subcommands:
  query          List a resource (alerts|cases|endpoints) with optional filter
                  query params. Cursor-paginates up to --limit. Emits grouped
                  PSV with a ref for drill-down.
  view-result     Drill into one row (full entity JSON) from a prior query.
  get             Hydrate a single entity by id (no ref needed).
  upload-samples  Verify-then-upload sampled entities to the knowledge graph.
  refresh         Walk each connector's catalog (Resource -> Field) into the KG.
  ls              List this skill's Sophos connectors' database schema by path.
  grep            String-search this skill's connectors' KG catalog.

DRAFT NOTE — endpoints, resource paths, and the ``items``/``pages.nextKey``
cursor shape are from the Sophos Central API docs and were reachability-checked
against a live (empty) trial tenant, but pagination and field shapes have not
been exercised with real data. SIEM events (`/siem/v1/events`, requires
`limit>=200`) and the async Detections query-job API are intentionally deferred
to a later version. Reconcile the per-resource shapes in ``RESOURCES`` against a
populated tenant during field-testing.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

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

SKILL_NAME = "sophos_central"
PACKAGE = "sophos_central"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "sophos_central.py view-result"
# Sophos Central's REST filter grammar has no queryerror entry; hints degrade to
# the raw upstream message.
QUERY_INTERFACE = "rest"
# Sophos caps most collection endpoints at pageSize=500; that's the vendor's
# own per-request ceiling, not this connector's result-size ceiling (see
# DEFAULT_LIMIT/HARD_CAP below, per CONTRIBUTING.md "Every list/query command
# has a code-enforced result-size ceiling").
MAX_PAGE_SIZE = 500
DEFAULT_LIMIT = 25
HARD_CAP = 100


@dataclass(frozen=True)
class _Resource:
    """A Sophos Central collection endpoint.

    ``list_path`` is the collection path (get-by-id appends ``/{id}``). All three
    v1 resources share the modern Central shape: a top-level ``items`` array plus
    a ``pages`` object whose ``nextKey`` drives cursor pagination via the
    ``pageFromKey`` query parameter.
    """

    name: str
    list_path: str


RESOURCES: dict[str, _Resource] = {
    # Common API — cross-product alerts.
    "alerts": _Resource(name="alerts", list_path="/common/v1/alerts"),
    # Cases API — investigations/cases (the Fusion analyst work-item).
    "cases": _Resource(name="cases", list_path="/cases/v1/cases"),
    # Endpoint API — managed device inventory; identity fields are the
    # cross-connector join keys.
    "endpoints": _Resource(name="endpoints", list_path="/endpoint/v1/endpoints"),
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


def _parse_filter(filter_str: str) -> dict[str, str]:
    """Parse a raw ``k=v&k2=v2`` filter string into query params.

    Sophos filtering is per-endpoint query parameters (e.g. alerts
    ``groupKey``/``severity``, endpoints ``healthStatus``/``search``), so the
    CLI passes them through verbatim rather than inventing a filter grammar.
    """
    filter_str = filter_str.strip()
    if not filter_str:
        return {}
    return {k: v for k, v in parse_qsl(filter_str, keep_blank_values=False)}


def _search(
    client: mcp.MCPClient, connector: str, resource: str, filter_str: str, limit: int
) -> list[dict[str, Any]]:
    """Cursor-paginate a Sophos Central collection up to ``limit`` rows.

    Follows ``pages.nextKey`` via the ``pageFromKey`` query parameter until
    ``limit`` is reached, a page is empty, or no ``nextKey`` remains.

    ``limit`` is clamped to HARD_CAP before the loop starts (the CLI's own
    click.IntRange already enforces this for `query`, but this function is
    also reachable directly from the shared `sample`/`lookup` commands,
    whose own --limit is not IntRange-bounded). Each request's `pageSize`
    is already capped per-page at MAX_PAGE_SIZE (Sophos's own 500-row
    ceiling); clamping `limit` itself is what bounds the number of pages
    fetched.
    """
    limit = min(limit, HARD_CAP)
    spec = RESOURCES[resource]
    base_query = _parse_filter(filter_str)
    rows: list[dict[str, Any]] = []
    next_key: str | None = None
    while len(rows) < limit:
        query = dict(base_query)
        query["pageSize"] = str(min(limit - len(rows), MAX_PAGE_SIZE))
        if next_key:
            query["pageFromKey"] = next_key
        r = proxy.dispatch(
            client, connector, method="GET", path=spec.list_path, query=query
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface=QUERY_INTERFACE,
                verb=f"{resource} query",
                connector=connector,
                package=PACKAGE,
                query=filter_str or None,
                response=r,
                candidates_path=[resource],
                client=client,
            )
        payload = json.loads(r.body)
        page = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(page, list):
            rows.extend(row for row in page if isinstance(row, dict))
        else:
            break
        pages = payload.get("pages") if isinstance(payload, dict) else None
        next_key = pages.get("nextKey") if isinstance(pages, dict) else None
        if not next_key or not page:
            break
    return rows[:limit]


def _get_entity(
    client: mcp.MCPClient, connector: str, resource: str, entity_id: str
) -> dict[str, Any] | None:
    spec = RESOURCES[resource]
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=f"{spec.list_path}/{entity_id}",
    )
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface=QUERY_INTERFACE,
            verb=f"get {resource}",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=[resource],
            client=client,
        )
    payload = json.loads(r.body)
    return payload if isinstance(payload, dict) else None


class _SophosSchemaProvider:
    """SchemaProvider for Sophos Central: resource -> field.

    Field names are discovered empirically from a one-row sample of each
    resource (its top-level JSON keys), matching the Defender connector. Endpoint
    identity fields learned here (hostname, ipv4Addresses, id) are the join keys
    that tie an alert or case back to a device.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Sophos"
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
        rows = _search(self._client, connector, resource, "", 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


_EXAMPLES = """
Examples:

\b
  sophos_central.py query --resource alerts --filter "severity=high" --limit 50
  sophos_central.py query --resource endpoints --filter "healthStatus=bad" --limit 50
  sophos_central.py query --resource cases --limit 25
  sophos_central.py get --resource endpoints --id <endpoint-id>
  sophos_central.py view-result --ref <ref> --row 1
  sophos_central.py grep --pattern hostname
  sophos_central.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Sophos Central (Fusion) connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(sorted(RESOURCE_NAMES)),
    default="alerts",
    show_default=True,
    help="Sophos Central resource to query.",
)
@click.option(
    "--filter",
    "filter_str",
    default="",
    help="Raw Sophos query parameters as a `k=v&k2=v2` string (e.g. "
    "\"severity=high\" for alerts, \"healthStatus=bad\" for endpoints). "
    "Empty returns all rows up to --limit.",
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
    """Run a list query and emit grouped PSV with a ref UUID for drill-down."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search(client, connector, resource, filter_str, limit)

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

    Sophos Central returns full entities at list time, so this is a pure cache
    read — no second backend call needed.
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
    help="Sophos Central resource the id belongs to.",
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
    """KG sample/lookup over a Sophos resource, in the canonical row shape
    upload-samples upserts: entity objects from ``_search`` (the same fetch
    ``query`` uses, no transform). A column lookup maps to a single Sophos query
    parameter (``column=value``); multi-value lookups aren't expressible as one
    param, so they fall back to an unfiltered sample. A path that is not a single
    known resource returns [].
    """
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    resource = segments[0]
    filter_str = ""
    if column is not None and len(values) == 1:
        filter_str = f"{column}={values[0]}"
    return _search(client, connector, resource, filter_str, limit)


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _SophosSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Sophos",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Sophos Central connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Sophos Central connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\none-row sample of each resource.",
    refresh_docstring="Walk Sophos Central alerts/cases/endpoints schema in real time and commit catalog tiers to the KG.\n\nThe only ``sophos_central`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Sophos Central connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --filter the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `sophos_central.py query --output …`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /alerts/<field> or /endpoints/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (filter), the export\ncannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
