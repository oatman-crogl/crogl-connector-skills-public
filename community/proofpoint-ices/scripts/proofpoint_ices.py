#!/usr/bin/env python3
"""proofpoint_ices CLI — drives a Proofpoint Email Security (ICES) custom
connector via the MCP api_proxy tool.

Proofpoint ICES (the rebranded Tessian product line — Defender, Guardian,
Architect modules) is a tenant-scoped REST API, distinct from the older
Proofpoint TAP/secure-email-gateway product. The connector's base URL is the
tenant's Proofpoint Portal host (e.g. https://yourcompany.tessian-app.com);
crogld attaches the `Authorization: API-Token <token>` header server-side from
the stored api_token secret. This container never holds the token.

Subcommands:
  query               List a resource (events|groups|users|anomalies|audits)
                       with an optional raw filter string. Checkpoint-
                       paginates up to --limit. Emits grouped PSV with a ref
                       for drill-down.
  view-result          Drill into one row (full entity JSON) from a prior
                       query.
  get                  Hydrate a single group by id, including its member
                       list (the only resource with a get-by-id endpoint).
  get-policy-parameter  Read a Custom Policy Configuration parameter value
                       (currently only `keywords_list` is documented).
  upload-samples        Verify-then-upload sampled entities to the KG.
  refresh, ls, grep     Standard KG catalog commands.

Read-only by design: this connector does not expose the API's remediation
(quarantine/inbox delete) or group/policy write endpoints.

DRAFT NOTE — endpoints, auth, and schemas below are transcribed directly from
the live Proofpoint-hosted OpenAPI spec at
https://developer.tessian.com/documentation/api/index.html (fetched
2026-07-29), not from training-data memory. This has NOT been live-tested
against a real Proofpoint tenant — there is no test tenant/credentials for
this connector yet. Treat any behavior not explicitly quoted from the spec
(exact rate-limit numbers, the precise Portal menu path) as best-effort; see
SKILL.md for what is and isn't confirmed.
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

SKILL_NAME = "proofpoint_ices"
PACKAGE = "proofpoint_ices"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "proofpoint_ices.py view-result"
# The Proofpoint API's filter grammar has no queryerror entry (it isn't a
# query language — just per-endpoint query params); hints degrade to the raw
# upstream message.
QUERY_INTERFACE = "rest"

# Context-budget rules (mirror the SKILL.md), matching this repo's convention
# for query-style connectors (e.g. qradar, sophos_central).
DEFAULT_LIMIT = 5
HARD_CAP = 25
# The API's own per-request page-size ceiling varies by endpoint (events caps
# at 100; groups/users/audits/anomalies allow up to 1000-2000). 100 is safe
# for all of them and comfortably above HARD_CAP.
MAX_PAGE_SIZE = 100

CHECKPOINT_PARAM = "after_checkpoint"


@dataclass(frozen=True)
class _Resource:
    """A Proofpoint ICES collection endpoint.

    All list endpoints share the checkpoint-cursor shape (a `checkpoint`
    string plus a boolean "more results" flag), but the exact key names for
    the row array and the "more" flag are NOT uniform across endpoints —
    `anomalies` uses `data`/`has_more` where every other resource uses
    `results` (or `groups`)/`additional_results`. Confirmed from the live spec.
    """

    name: str
    path: str
    list_key: str
    more_key: str


RESOURCES: dict[str, _Resource] = {
    # Security Events — from Defender, Guardian, and Architect modules.
    "events": _Resource(name="events", path="/api/v1/events", list_key="results", more_key="additional_results"),
    # User Groups — named collections of addresses/domains.
    "groups": _Resource(name="groups", path="/api/v1/groups", list_key="groups", more_key="additional_results"),
    # User Monitoring — deployment/coverage status per user.
    "users": _Resource(name="users", path="/api/v1/monitoring/users", list_key="results", more_key="additional_results"),
    # Email Exfiltration Anomalies. NOTE: different envelope shape (data/has_more,
    # not results/additional_results) and a day-scoped checkpoint quirk — see
    # SKILL.md before assuming this behaves like the other resources.
    "anomalies": _Resource(name="anomalies", path="/reporting/anomalies/v1", list_key="data", more_key="has_more"),
    # Audit Trail — usage/config-change log.
    "audits": _Resource(name="audits", path="/api/v1/audits", list_key="results", more_key="additional_results"),
}
RESOURCE_NAMES = tuple(RESOURCES.keys())

# Only `groups` has a get-by-id endpoint (GET /api/v1/groups/{id}). Events,
# users, anomalies, and audits are list-only in this API — there is no
# per-item hydrate call for them.
GET_RESOURCE_NAMES = ("groups",)


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

    The API's filtering is per-endpoint query parameters (only `events`
    documents one: `created_after`, an ISO 8601 timestamp), so the CLI passes
    params through verbatim rather than inventing a filter grammar.
    """
    filter_str = filter_str.strip()
    if not filter_str:
        return {}
    return {k: v for k, v in parse_qsl(filter_str, keep_blank_values=False)}


def _search(
    client: mcp.MCPClient, connector: str, resource: str, filter_str: str, limit: int
) -> list[dict[str, Any]]:
    """Checkpoint-paginate a Proofpoint ICES collection up to ``limit`` rows.

    Follows the `checkpoint`/"more results" envelope via the
    `after_checkpoint` query parameter until `limit` is reached, a page is
    empty, or no further results remain. See `_Resource` for why the response
    key names are looked up per-resource rather than assumed uniform.
    """
    spec = RESOURCES[resource]
    base_query = _parse_filter(filter_str)
    rows: list[dict[str, Any]] = []
    checkpoint: str | None = None
    while len(rows) < limit:
        query = dict(base_query)
        query["limit"] = str(min(limit - len(rows), MAX_PAGE_SIZE))
        if checkpoint:
            query[CHECKPOINT_PARAM] = checkpoint
        r = proxy.dispatch(client, connector, method="GET", path=spec.path, query=query)
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
        page = payload.get(spec.list_key) if isinstance(payload, dict) else None
        if isinstance(page, list):
            rows.extend(row for row in page if isinstance(row, dict))
        else:
            break
        checkpoint = payload.get("checkpoint") if isinstance(payload, dict) else None
        more = payload.get(spec.more_key) if isinstance(payload, dict) else None
        if not more or not page:
            break
    return rows[:limit]


def _get_group(client: mcp.MCPClient, connector: str, group_id: str, limit: int) -> dict[str, Any] | None:
    """Hydrate one group, including up to `limit` of its members.

    Unlike the flat list shape `query --resource groups` returns, this
    endpoint's response nests `metadata` + `members` under `results`, and
    `members` is itself checkpoint-paginated (its own `checkpoint`/
    `additional_results`, not followed further here — a 404 means no such
    group id).
    """
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=f"/api/v1/groups/{group_id}",
        query={"limit": str(limit)},
    )
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface=QUERY_INTERFACE,
            verb="get group",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=["groups"],
            client=client,
        )
    payload = json.loads(r.body)
    return payload if isinstance(payload, dict) else None


def _get_policy_parameter(client: mcp.MCPClient, connector: str, parameter_id: str) -> dict[str, Any]:
    """Read a Custom Policy Configuration parameter (e.g. `keywords_list`)."""
    r = proxy.dispatch(client, connector, method="GET", path=f"/filters/parameters/v1/{parameter_id}")
    if r.status_code == 404:
        raise click.ClickException(f"parameter_id {parameter_id!r} not recognised or not found.")
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface=QUERY_INTERFACE,
            verb="get policy parameter",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=None,
            client=client,
        )
    payload = json.loads(r.body)
    return payload if isinstance(payload, dict) else {}


class _ProofpointSchemaProvider:
    """SchemaProvider for Proofpoint ICES: resource -> field.

    Field names are discovered empirically from a one-row sample of each
    resource's list endpoint (its top-level JSON keys), matching the
    Sophos Central connector's pattern.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Proofpoint"
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
  proofpoint_ices.py query --resource events --filter "created_after=2026-07-28T00:00:00Z" --limit 25
  proofpoint_ices.py query --resource groups --limit 25
  proofpoint_ices.py query --resource anomalies --limit 25
  proofpoint_ices.py get --resource groups --id 42
  proofpoint_ices.py get-policy-parameter --parameter-id keywords_list
  proofpoint_ices.py view-result --ref <ref> --row 1
  proofpoint_ices.py grep --pattern intent_types
  proofpoint_ices.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Proofpoint Email Security (ICES) connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(sorted(RESOURCE_NAMES)),
    default="events",
    show_default=True,
    help="Proofpoint ICES resource to query.",
)
@click.option(
    "--filter",
    "filter_str",
    default="",
    help="Raw query parameters as a `k=v&k2=v2` string. Only `events` documents "
    "a filter param: `created_after` (ISO 8601 timestamp). Other resources take "
    "no filter — empty returns all rows up to --limit.",
)
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
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
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=int, help="Row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one entity from a prior query (pure cache read)."""
    try:
        entity = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(entity, indent=2))


@cli.command(name="get")
@click.option(
    "--resource",
    type=click.Choice(GET_RESOURCE_NAMES),
    default="groups",
    show_default=True,
    help="Only `groups` has a get-by-id endpoint in this API.",
)
@click.option("--id", "entity_id", required=True, help="Group id to hydrate.")
@click.option("--limit", default=HARD_CAP, show_default=True, type=int, help="Max members to include (this endpoint paginates members separately).")
def get(resource: str, entity_id: str, limit: int) -> None:
    """Hydrate a single group by id, including its member list."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        entity = _get_group(client, connector, entity_id, limit)
    if entity is None:
        raise click.ClickException(f"no group found for id {entity_id!r}")
    click.echo(json.dumps(entity, indent=2))


@cli.command(name="get-policy-parameter")
@click.option(
    "--parameter-id",
    default="keywords_list",
    show_default=True,
    help="Custom Policy Configuration parameter id. `keywords_list` is the only one documented in the spec.",
)
def get_policy_parameter(parameter_id: str) -> None:
    """Read a Custom Policy Configuration parameter's current value."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get_policy_parameter(client, connector, parameter_id)
    click.echo(json.dumps(payload, indent=2))


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a Proofpoint ICES resource, in the canonical row
    shape upload-samples upserts: entity objects from ``_search`` (the same
    fetch ``query`` uses, no transform). A column lookup maps to a single
    `k=v` filter param; multi-value lookups aren't expressible as one param
    (and most resources accept no filter at all besides `events`'
    `created_after`), so they fall back to an unfiltered sample. A path that
    is not a single known resource returns [].
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
    lambda client: _ProofpointSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Proofpoint",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Proofpoint ICES connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Proofpoint ICES connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\none-row sample of each resource.",
    refresh_docstring="Walk Proofpoint ICES events/groups/users/anomalies/audits schema in real time and commit catalog tiers to the KG.\n\nThe only ``proofpoint_ices`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Proofpoint ICES connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --filter the samples came from (must match the source search; pass an empty string if the search used no filter).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `proofpoint_ices.py query --output …`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /events/<field> or /groups/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (filter), the export\ncannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
