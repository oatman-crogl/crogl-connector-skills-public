#!/usr/bin/env python3
"""crogl-intune CLI — drives a Microsoft Intune custom connector via the
MCP api_proxy tool (Microsoft Graph deviceManagement surface).

Subcommands:
  query          OData $filter search over a resource (managed-devices).
                  Paginates via @odata.nextLink up to --limit. Emits grouped
                  PSV with a ref for drill-down.
  view-result     Drill into one row (full entity JSON) from a prior query.
  get             Hydrate a single entity by id (no ref needed).
  upload-samples  Verify-then-upload sampled entities to the knowledge graph.
  refresh         Walk each connector's catalog (Resource -> Field) into the KG.
  ls              List this skill's Intune connectors' database schema by UNIX path.
  grep            String-search this skill's connectors' KG catalog.

The configured custom connector's base URL should be the Graph API root
(https://graph.microsoft.com). The CLI sends absolute Graph paths with an
explicit API version per resource; auth (an OAuth client_credentials Bearer)
is applied server-side from the connector's stored secret.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

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

SKILL_NAME = "intune"
PACKAGE = "intune"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "intune.py view-result"
BASE_URL = "https://graph.microsoft.com"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). The one resource returns full
# entities in one page-sized response with no separate bulk-listing
# command, so one ceiling applies. MAX_PAGE_SIZE is the explicit $top sent
# on every request -- capped at HARD_CAP since no query here ever needs
# more than one page's worth beyond that.
DEFAULT_LIMIT = 25
HARD_CAP = 100
MAX_PAGE_SIZE = HARD_CAP

# Per-resource Graph path, versioned individually since Intune's
# deviceManagement surface spans both /v1.0 and /beta.
RESOURCES: dict[str, dict[str, str]] = {
    "managed-devices": {"list_path": "/v1.0/deviceManagement/managedDevices"},
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


def _next_page(next_link: str) -> tuple[str, dict[str, str]]:
    """Split an absolute @odata.nextLink into a relative path + query dict.

    proxy.dispatch's `path` is always joined literally onto the connector's
    base_url server-side (it is never re-parsed as its own URL), so an
    absolute nextLink must be reduced to base_url-relative path + query
    before being passed through.
    """
    if not next_link.startswith(BASE_URL):
        raise click.ClickException(f"unexpected @odata.nextLink host: {next_link}")
    parsed = urlsplit(next_link)
    return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}


def _search(
    client: mcp.MCPClient, connector: str, resource: str, odata_filter: str, limit: int
) -> list[dict[str, Any]]:
    """Single-stage Graph fetch with @odata.nextLink pagination up to limit.

    Unlike a two-stage query-ids -> hydrate-entities API, Graph's
    deviceManagement collection endpoints return full entities directly;
    pagination is the only multi-request concern.

    `limit` is clamped to HARD_CAP before the loop starts (the CLI's own
    click.IntRange already enforces this for `query`, but this function is
    also reachable directly from the shared `sample`/`lookup` commands,
    whose own --limit is not IntRange-bounded). Each request explicitly caps
    the page it asks for via $top rather than relying on Graph's own default
    page size.
    """
    limit = min(limit, HARD_CAP)
    spec = RESOURCES[resource]
    path = spec["list_path"]
    query: dict[str, str] = {"$filter": odata_filter} if odata_filter else {}
    rows: list[dict[str, Any]] = []
    while path and len(rows) < limit:
        query["$top"] = str(min(limit - len(rows), MAX_PAGE_SIZE))
        r = proxy.dispatch(client, connector, method="GET", path=path, query=query)
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="odata",
                verb=f"{resource} query",
                connector=connector,
                package=PACKAGE,
                query=odata_filter,
                response=r,
                candidates_path=[resource],
                client=client,
            )
        payload = json.loads(r.body)
        page = payload.get("value") if isinstance(payload, dict) else None
        if isinstance(page, list):
            rows.extend(row for row in page if isinstance(row, dict))
        next_link = (
            payload.get("@odata.nextLink") if isinstance(payload, dict) else None
        )
        if not next_link:
            break
        path, query = _next_page(next_link)
    return rows[:limit]


class _IntuneSchemaProvider:
    """SchemaProvider for Intune: resource -> field.

    Graph has no createmeta-style schema endpoint, so the leaf field tier is
    discovered empirically: a one-row sample of the resource, whose
    top-level JSON keys are the catalog fields.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Intune"
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
        rows = _search(self._client, connector, resource, "", 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


_EXAMPLES = """
Examples:

\b
  intune.py query --resource managed-devices --filter "complianceState eq 'noncompliant'" --limit 50
  intune.py get --resource managed-devices --id <id>
  intune.py view-result --ref <ref> --row 1
  intune.py grep --pattern hostname
  intune.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Microsoft Intune connector via Microsoft Graph. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(sorted(RESOURCE_NAMES)),
    default="managed-devices",
    show_default=True,
    help="Intune resource to query.",
)
@click.option(
    "--filter",
    "odata_filter",
    default="",
    help="OData $filter expression (e.g. \"operatingSystem eq 'Windows' and "
    "complianceState eq 'noncompliant'\"). Empty returns all rows up to --limit.",
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
    odata_filter: str,
    limit: int,
    output_path: str | None,
) -> None:
    """Run an OData search and emit grouped PSV with a ref UUID for drill-down."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search(client, connector, resource, odata_filter, limit)

    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/query"

    if not rows:
        click.echo("")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            resource=resource,
            filter=odata_filter,
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
        query=odata_filter,
        catalog_path=[resource],
    )

    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=odata_filter,
            ref=ref,
            rows=rows,
        )

    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        resource=resource,
        filter=odata_filter,
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

    Graph entities are returned in full at search time, so this is a pure
    cache read — no second backend call needed.
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
    default="managed-devices",
    show_default=True,
    help="Intune resource the id belongs to.",
)
@click.option("--id", "entity_id", required=True, help="Entity id to hydrate.")
@click.pass_context
def get(ctx: click.Context, resource: str, entity_id: str) -> None:
    """Hydrate a single entity by id (no prior query ref needed)."""
    spec = RESOURCES[resource]
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=f"{spec['list_path']}/{entity_id}",
        )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="odata",
            verb=f"get {resource}",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=[resource],
            client=client,
        )
    click.echo(json.dumps(json.loads(r.body), indent=2))


def _odata_in_filter(column: str, values: list[str]) -> str:
    """OData has no native in-list operator for `eq`; chain with `or`."""
    return " or ".join(f"{column} eq '{v}'" for v in values)


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over an Intune resource, in the canonical row shape
    upload-samples upserts: entity objects from `_search` (the same
    paginated fetch `query` uses, no transform), so the sampled columns line
    up with the KG field nodes. A path that is not a single known resource
    returns []."""
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    resource = segments[0]
    if column is not None:
        if not values:
            return []
        odata_filter = _odata_in_filter(column, values)
    else:
        odata_filter = ""
    return _search(client, connector, resource, odata_filter, limit)


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _IntuneSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Intune",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Intune connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Intune connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\none-row sample of each resource.",
    refresh_docstring="Walk Intune device schema in real time and commit catalog tiers to the KG.\n\nThe only ``crogl-intune`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Intune connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact OData $filter the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `intune.py query --output …`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /managed-devices/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (OData filter), the\nexport cannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
