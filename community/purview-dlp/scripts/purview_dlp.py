#!/usr/bin/env python3
"""crogl-purview-dlp CLI — drives a Microsoft Purview DLP custom connector via
the MCP api_proxy tool (Microsoft Graph security/alerts_v2 surface, scoped to
Data Loss Prevention alerts).

Subcommands:
  query          OData $filter search over dlp-alerts. Paginates via
                  @odata.nextLink up to --limit. Emits grouped PSV with a ref
                  for drill-down.
  view-result     Drill into one row (full entity JSON) from a prior query.
  get             Hydrate a single alert by id (no ref needed).
  upload-samples  Verify-then-upload sampled entities to the knowledge graph.
  refresh         Walk each connector's catalog (Resource -> Field) into the KG.
  ls              List this skill's Purview DLP connectors' database schema by
                  UNIX path.
  grep            String-search this skill's connectors' KG catalog.

The configured custom connector's base URL should be the Graph API root
(https://graph.microsoft.com). The CLI sends absolute Graph paths with an
explicit API version; auth (an OAuth client_credentials Bearer) is applied
server-side from the connector's stored secret.

Every query is pinned to `serviceSource eq 'dataLossPrevention'` — this
connector only ever returns Purview DLP alerts, never the broader
Microsoft Graph security alert population from other providers.
"""

from __future__ import annotations

import json
import re
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

SKILL_NAME = "purview-dlp"
PACKAGE = "purview_dlp"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "purview_dlp.py view-result"
BASE_URL = "https://graph.microsoft.com"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). dlp-alerts is the only resource, with
# no separate bulk-listing command, so one ceiling applies everywhere.
# MAX_PAGE_SIZE is the explicit $top sent on every request -- capped at
# HARD_CAP since no query here ever needs more than one page's worth beyond
# that.
DEFAULT_LIMIT = 25
HARD_CAP = 100
MAX_PAGE_SIZE = HARD_CAP

# This connector implements exactly one resource: DLP-sourced alerts from the
# unified Graph security alerts_v2 surface. Every request is pinned to
# serviceSource eq 'dataLossPrevention'; see _dlp_filter below.
RESOURCES: dict[str, dict[str, str]] = {
    "dlp-alerts": {"list_path": "/v1.0/security/alerts_v2"},
}
RESOURCE_NAMES = tuple(RESOURCES.keys())

DLP_SERVICE_SOURCE_FILTER = "serviceSource eq 'dataLossPrevention'"


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _validate_filter_parens(user_filter: str) -> None:
    """Reject a $filter fragment whose parentheses aren't genuinely balanced
    outside of OData string literals.

    `_dlp_filter` below wraps the caller's fragment in one pair of parens to
    safely AND it with the mandatory DLP-scope clause. That's only safe if
    the fragment's own parens nest correctly -- otherwise a fragment like
    `"x eq 'y') or (true"` recombines with the parens `_dlp_filter` adds
    into `(x eq 'y') or (true) and serviceSource eq 'dataLossPrevention'`,
    which no longer requires the DLP clause at all (CONTRIBUTING.md rule 5
    point 5). A stack-based check (never let the open-paren count go
    negative, end at zero) catches exactly this: the malicious fragment
    above starts with an unmatched `)`, which a validly-nested fragment
    never does. OData string literals are single-quoted with `''` as an
    escaped embedded quote, so a paren inside a string literal isn't a real
    paren and is skipped rather than counted.
    """
    depth = 0
    in_string = False
    i = 0
    n = len(user_filter)
    while i < n:
        ch = user_filter[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and user_filter[i + 1] == "'":
                    i += 2  # escaped quote inside the literal
                    continue
                in_string = False
        elif ch == "'":
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise click.BadParameter(
                    "has an unmatched ')' -- this could recombine with the "
                    "DLP-scope clause this connector always ANDs on and "
                    "change what the query actually matches. Balance the "
                    "parentheses in --filter.",
                    param_hint="--filter",
                )
        i += 1
    if in_string:
        raise click.BadParameter(
            "has an unterminated string literal (unmatched \"'\").",
            param_hint="--filter",
        )
    if depth != 0:
        raise click.BadParameter(
            "has unbalanced parentheses -- this could recombine with the "
            "DLP-scope clause this connector always ANDs on and change "
            "what the query actually matches. Balance the parentheses in "
            "--filter.",
            param_hint="--filter",
        )


def _dlp_filter(user_filter: str) -> str:
    """AND the caller's $filter with the fixed DLP service-source scope.

    This is what keeps `purview-dlp` a DLP-only connector rather than a
    general alerts_v2 client -- every request this CLI sends carries this
    clause, so a caller can never widen a query to see non-DLP alerts. This
    only holds if the caller's fragment can't smuggle its own top-level
    grouping past the parens added here -- see _validate_filter_parens.
    """
    if not user_filter:
        return DLP_SERVICE_SOURCE_FILTER
    _validate_filter_parens(user_filter)
    return f"({user_filter}) and {DLP_SERVICE_SOURCE_FILTER}"


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

    Graph's alerts_v2 collection endpoint returns full alert entities
    directly (no separate hydrate step); pagination is the only
    multi-request concern.

    `limit` is clamped to HARD_CAP before the loop starts (the CLI's own
    click.IntRange already enforces this for `query`, but this function is
    also reachable directly from the shared `sample`/`lookup` commands,
    whose own --limit is not IntRange-bounded). Each request explicitly caps
    the page it asks for via $top rather than relying on the API's own
    default page size.
    """
    limit = min(limit, HARD_CAP)
    spec = RESOURCES[resource]
    path = spec["list_path"]
    query: dict[str, str] = {"$filter": _dlp_filter(odata_filter)}
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


class _PurviewDlpSchemaProvider:
    """SchemaProvider for Purview DLP: resource -> field.

    Graph has no createmeta-style schema endpoint, so the leaf field tier is
    discovered empirically: a one-row sample of the resource, whose
    top-level JSON keys are the catalog fields.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Purview DLP"
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
  purview_dlp.py query --filter "severity eq 'high'" --limit 50
  purview_dlp.py get --id <alert-id>
  purview_dlp.py view-result --ref <ref> --row 1
  purview_dlp.py grep --pattern sharepoint
  purview_dlp.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Microsoft Purview DLP connector via Microsoft Graph. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(sorted(RESOURCE_NAMES)),
    default="dlp-alerts",
    show_default=True,
    help="Purview DLP resource to query.",
)
@click.option(
    "--filter",
    "odata_filter",
    default="",
    help="OData $filter expression over the documented filterable properties "
    "(assignedTo, classification, determination, createdDateTime, "
    "lastUpdateDateTime, severity, status) -- e.g. \"severity eq 'high'\". "
    "Always ANDed server-side with serviceSource eq 'dataLossPrevention'; "
    "empty returns all DLP alerts up to --limit.",
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
    """Run an OData search over DLP alerts and emit grouped PSV with a ref UUID for drill-down."""
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
    """Print the full JSON for one alert from a prior query.

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
    default="dlp-alerts",
    show_default=True,
    help="Purview DLP resource the id belongs to.",
)
@click.option("--id", "entity_id", required=True, help="Alert id to hydrate.")
@click.pass_context
def get(ctx: click.Context, resource: str, entity_id: str) -> None:
    """Hydrate a single DLP alert by id (no prior query ref needed).

    Unlike `query`, this does not re-check serviceSource -- Graph's get-by-id
    has no $filter support, so a caller who already has a concrete alert id
    (e.g. from an incident cross-reference) can fetch it directly. This is
    the one path where a non-DLP alert id could technically return
    non-DLP data; it's still scoped to the alerts_v2 resource, just not to
    the DLP service-source filter.
    """
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


_COLUMN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _odata_quote(value: str) -> str:
    """Escape a value for an OData string literal (embedded `'` doubles, per
    the OData ABNF -- OData has no backslash-escape convention)."""
    return "'" + value.replace("'", "''") + "'"


def _odata_in_filter(column: str, values: list[str]) -> str:
    """OData has no native in-list operator for `eq`; chain with `or`.

    `column` comes straight from the shared `lookup` subcommand's --column
    (CONTRIBUTING.md rule 5): there's no fixed field list to allowlist
    against here (fields are discovered empirically per resource --
    see _PurviewDlpSchemaProvider -- not enumerated in code), so it's
    checked against a conservative identifier pattern instead. Graph
    property names on this resource are plain identifiers; anything else is
    refused rather than spliced into the filter unchecked. `values` are
    escaped per OData string-literal rules, not just wrapped in quotes.
    """
    if not _COLUMN_RE.match(column):
        raise click.BadParameter(
            f"{column!r} is not a plain identifier -- refusing to build a "
            "filter from it.",
            param_hint="--column",
        )
    return " or ".join(f"{column} eq {_odata_quote(v)}" for v in values)


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over the dlp-alerts resource, in the canonical row
    shape upload-samples upserts: entity objects from `_search` (the same
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
    lambda client: _PurviewDlpSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Purview DLP",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Purview DLP connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Purview DLP connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\none-row sample of the resource.",
    refresh_docstring="Walk Purview DLP alert schema in real time and commit catalog tiers to the KG.\n\nThe only ``crogl-purview-dlp`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Purview DLP connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact OData $filter the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `purview_dlp.py query --output …`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /dlp-alerts/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (OData filter), the\nexport cannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
