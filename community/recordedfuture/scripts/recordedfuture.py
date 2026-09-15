#!/usr/bin/env python3
"""recordedfuture CLI -- drives a Recorded Future community connector through
the MCP api_proxy tool, covering the read-only lookup/search surface that
shares Recorded Future's static X-RFToken API-key auth: entity intelligence
(company/domain/hash/ip/url/malware/vulnerability), threat actor & malware
search, alerts, playbook alerts, identity exposure lookups, links, and
entity match.

Subcommands:
  query           Search-style lookup over one resource (--resource), with
                  arbitrary --param KEY=VALUE filters and --limit.
  get             Hydrate one entity/alert/playbook-alert by id.
  riskrules       List the risk-rule catalog for one entity type.
  metadata        Fetch a small fixed reference list (categories, statuses,
                  link section types, ...).
  view-result     Re-print a prior query's cached rows.
  upload-samples  Verify-then-upload sampled rows to the knowledge graph.
  refresh         Walk each resource's catalog (resource -> field) into the KG.
  ls              List this skill's Recorded Future connectors' schema by path.
  grep            String-search this skill's connectors' KG catalog.

Explicitly out of scope (separate future connector types, not wired into
RESOURCES below): Cases, Analyst Notes, Lists (custom list CRUD), Alert/
Playbook Alert *Update* (write/mutation), Detection Rules + YARA/Sigma job
creation, Collective Insights, Fusion Files, Malware Intelligence query
language, SOAR bulk enrichment, STIX/TAXII, Takedown API, Sandbox, ASI, PFI,
RiskRecon -- several of those live on entirely different hosts/auth and
cannot share this connector's credential regardless of CLI design.

Auth is server-side: the agent container provides CROGL_MCP_URL and
CROGL_CLI_TOKEN. This skill is bound to one configured connector and
resolves it automatically; you don't pass a connector name. The server
attaches the connector's stored X-RFToken header when forwarding upstream.
"""

from __future__ import annotations

import json
import re
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

SKILL_NAME = "recordedfuture"
PACKAGE = "recordedfuture"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "recordedfuture.py view-result"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). Vendor docs (docs.recordedfuture.com)
# document a max of 1000 for the Connect API's own `limit` field; HARD_CAP is
# this connector's own, stricter ceiling, independent of that higher vendor
# max. Two resources (identity-password-lookup, links) are excluded per
# CONTRIBUTING.md rule 4's vendor-limitation exception -- see their
# `no_limit` entries in RESOURCES below.
DEFAULT_LIMIT = 20
HARD_CAP = 100

# Entity types under the Connect API (/v2). Uniform shape: search, lookup by
# id, riskrules (all but malware -- Recorded Future does not risk-score
# malware families the way it does IOCs), and an Intelligence Card
# "extension" sub-query on lookup.
_ENTITY_TYPES = ("company", "domain", "hash", "ip", "url", "malware", "vulnerability")


def _entity_resource(entity_type: str) -> dict[str, Any]:
    return {
        "method": "GET",
        "search_path": f"/v2/{entity_type}/search",
        "search_rows": "data.results",
        "id_path": f"/v2/{entity_type}/{{id}}",
        "id_rows": "data",
        "riskrules_path": None if entity_type == "malware" else f"/v2/{entity_type}/riskrules",
        "extension_path": f"/v2/{entity_type}/{{id}}/extension/{{extension}}",
        "array_fields": frozenset(),
        "required_fields": frozenset(),
    }


# Resources this connector implements. Each entry declares how `query`/`get`
# reach the upstream API: GET resources take --param as raw querystring
# key=value pairs (matching Recorded Future's own string-encoded filter
# syntax, e.g. riskScore="[70,]"); POST resources take --param as JSON body
# fields, with `array_fields` naming which keys get comma-split into a JSON
# array rather than passed as a scalar. `search_rows`/`id_rows` is the
# dotted key path to the row(s) in the response envelope; see
# `_extract_rows` for exactly how that's resolved.
RESOURCES: dict[str, dict[str, Any]] = {
    **{t: _entity_resource(t) for t in _ENTITY_TYPES},
    "actor": {
        "method": "POST",
        "search_path": "/threat/actor/search",
        "search_rows": "data",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset(),
        "required_fields": frozenset(),
    },
    "alert": {
        "method": "GET",
        "search_path": "/alert/v3",
        "search_rows": "data",
        "id_path": "/alert/v3/{id}",
        "id_rows": "data",
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset(),
        "required_fields": frozenset(),
    },
    "playbook-alert": {
        "method": "POST",
        "search_path": "/playbook-alert/search",
        "search_rows": "data",
        "id_path": "/playbook-alert/common/{id}",
        "id_rows": "data",
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset(
            {"entity", "statuses", "priority", "category", "assignee", "organisation"}
        ),
        "required_fields": frozenset(),
    },
    "identity-credentials-search": {
        "method": "POST",
        "search_path": "/identity/credentials/search",
        "search_rows": "identities",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset({"domains", "domain_types"}),
        "required_fields": frozenset(),
    },
    "identity-credentials-lookup": {
        "method": "POST",
        "search_path": "/identity/credentials/lookup",
        "search_rows": "identities",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset({"subjects", "subjects_sha1", "subjects_login"}),
        "required_fields": frozenset(),
    },
    "identity-password-lookup": {
        "method": "POST",
        "search_path": "/identity/password/lookup",
        "search_rows": "results",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset({"passwords"}),
        "required_fields": frozenset({"passwords"}),
        # CONTRIBUTING.md rule 4's vendor-limitation exception: this is a
        # lookup-by-explicit-list endpoint (one result per submitted password
        # hash), not an open search -- confirmed 2026-09-10 against
        # docs.recordedfuture.com that no limit/pagination field exists on
        # this endpoint; response size is bounded by the caller's own
        # --param passwords list, not by an upstream match count.
        "no_limit": True,
    },
    "identity-hostname-lookup": {
        "method": "POST",
        "search_path": "/identity/hostname/lookup",
        "search_rows": "identities",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset(),
        "required_fields": frozenset({"hostname"}),
    },
    "identity-ip-lookup": {
        "method": "POST",
        "search_path": "/identity/ip/lookup",
        "search_rows": "identities",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset(),
        "required_fields": frozenset(),
    },
    "links": {
        "method": "POST",
        "search_path": "/links/search",
        "search_rows": "data",
        "id_path": None,
        "id_rows": None,
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset({"entities"}),
        "required_fields": frozenset({"entities"}),
        # Same vendor-limitation exception as identity-password-lookup above
        # (confirmed 2026-09-10 against docs.recordedfuture.com): a
        # lookup-by-explicit-entity-list endpoint, no limit/pagination field
        # documented, response size bounded by the caller's own --param
        # entities list.
        "no_limit": True,
    },
    "entity-match": {
        "method": "POST",
        "search_path": "/entity-match/match",
        "search_rows": None,  # top-level array, not nested under a key
        "id_path": "/entity-match/entity/{id}",
        "id_rows": "data",
        "riskrules_path": None,
        "extension_path": None,
        "array_fields": frozenset({"type"}),
        "required_fields": frozenset({"name"}),
    },
}
RESOURCE_NAMES = tuple(sorted(RESOURCES.keys()))

# Small fixed reference-list endpoints -- not searchable/filterable, just a
# GET of a catalog used to populate other resources' filter values.
METADATA_ENDPOINTS: dict[str, str] = {
    "actor-categories": "/threat/actor/categories",
    "malware-categories": "/threat/malware/categories",
    "playbook-alert-common": "/playbook-alert/metadata/common",
    "links-events": "/links/metadata/events",
    "links-entities": "/links/metadata/entities",
    "links-sections": "/links/metadata/sections",
}

_JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _resource(name: str) -> dict[str, Any]:
    spec = RESOURCES.get(name)
    if spec is None:
        raise click.BadParameter(f"unknown resource: {name!r}", param_hint="--resource")
    return spec


def _coerce_scalar(v: str) -> Any:
    """Best-effort JSON typing for a POST-body --param value: true/false ->
    bool, all-digits -> int, otherwise left as a string."""
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _parse_params(pairs: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(
                f"expected KEY=VALUE, got {pair!r}", param_hint="--param"
            )
        k, v = pair.split("=", 1)
        out[k.strip()] = v
    return out


def _build_body(params: dict[str, str], array_fields: frozenset[str]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for k, v in params.items():
        if k in array_fields:
            body[k] = [_coerce_scalar(x.strip()) for x in v.split(",")]
        else:
            body[k] = _coerce_scalar(v)
    return body


def _dotted_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _extract_rows(payload: Any, rows_key: str | None) -> list[dict[str, Any]]:
    """Pull the row list out of a response envelope. ``rows_key=None`` means
    the payload itself is the array (entity-match's /match). Otherwise
    resolve the dotted key path; if what's there is a list use it as-is, if
    it's a single object wrap it as a one-row list (covers e.g. Links'
    /search, whose documented schema returns one object per queried entity
    but whose multi-entity behavior is not live-verified -- see SKILL.md)."""
    value = payload if rows_key is None else _dotted_get(payload, rows_key)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _search(
    client: mcp.MCPClient,
    connector: str,
    resource: str,
    params: dict[str, str],
    limit: int | None,
) -> list[dict[str, Any]]:
    """Single-request fetch (no pagination loop); `limit` is sent verbatim as
    the resource's own `limit` field, except for the two `no_limit`
    resources, which have no such field at all (see their RESOURCES entries).

    Clamped to HARD_CAP regardless of caller (the CLI's own click.IntRange
    already enforces this for `query`, but this function is also reachable
    directly from the shared `sample`/`lookup` commands, whose own --limit is
    not IntRange-bounded). Rows are also sliced to the clamped limit after
    the fetch -- defense in depth in case a resource ever returns more than
    its own `limit` field asked for.
    """
    spec = _resource(resource)
    missing = spec["required_fields"] - params.keys()
    if missing:
        raise click.ClickException(
            f"resource {resource!r} requires --param for: {', '.join(sorted(missing))}"
        )

    if limit is not None and not spec.get("no_limit"):
        limit = min(limit, HARD_CAP)
        params = {**params, "limit": str(limit)}

    if spec["method"] == "GET":
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=spec["search_path"],
            query=params,
            headers={"Accept": "application/json"},
        )
    else:
        body = _build_body(params, spec["array_fields"])
        r = proxy.dispatch(
            client,
            connector,
            method="POST",
            path=spec["search_path"],
            body=json.dumps(body),
            headers=_JSON_HEADERS,
        )

    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ioc",
            verb=f"{resource} query",
            connector=connector,
            package=PACKAGE,
            query=json.dumps(params),
            response=r,
            candidates_path=[resource],
            client=client,
        )
    payload = json.loads(r.body)
    rows = _extract_rows(payload, spec["search_rows"])
    if limit is not None and not spec.get("no_limit"):
        rows = rows[:limit]
    return rows


class _RecordedFutureSchemaProvider:
    """SchemaProvider for Recorded Future: resource -> field.

    None of these resources expose a createmeta-style field schema, so the
    leaf field tier is discovered empirically: an unfiltered, small-limit
    sample of the resource, whose top-level JSON keys become the catalog
    fields. Implements the contract in ``_lib/discover.py``.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Recorded Future"
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
        spec = RESOURCES.get(resource)
        if spec is None or spec["required_fields"]:
            # Resources with required filter fields (identity-password-lookup,
            # links, entity-match) can't be sampled with an empty filter.
            return []
        rows = _search(self._client, connector, resource, {}, 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


_EXAMPLES = """
Examples:

\b
  recordedfuture.py query --resource domain --param riskScore="[65,]" --limit 20
  recordedfuture.py query --resource actor --param name=Sandworm --limit 10
  recordedfuture.py query --resource playbook-alert --param category=domain_abuse --limit 20
  recordedfuture.py get --resource ip --id 8.8.8.8
  recordedfuture.py get --resource alert --id <alert_id>
  recordedfuture.py riskrules --resource domain
  recordedfuture.py metadata --resource playbook-alert-common
  recordedfuture.py view-result --ref <ref> --row 1
  recordedfuture.py grep --pattern riskRule
  recordedfuture.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Recorded Future connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(RESOURCE_NAMES),
    required=True,
    help="Recorded Future resource to search.",
)
@click.option(
    "--param",
    "params",
    multiple=True,
    help="Filter as KEY=VALUE; repeatable. See SKILL.md for each resource's fields.",
)
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows to fetch (hard cap {HARD_CAP}). Ignored for resources with "
    "no pagination concept (identity-password-lookup, links).",
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
    params: tuple[str, ...],
    limit: int,
    output_path: str | None,
) -> None:
    """Search one resource and emit grouped PSV with a ref UUID for drill-down."""
    parsed = _parse_params(params)
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search(client, connector, resource, parsed, limit)

    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/query"
    query_id = json.dumps(parsed, sort_keys=True)

    if not rows:
        click.echo("")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            resource=resource,
            params=parsed,
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
        query=query_id,
        catalog_path=[resource],
    )

    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=query_id,
            ref=ref,
            rows=rows,
        )

    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        resource=resource,
        params=parsed,
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
@click.option("--row", required=True, type=int, help="Row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query."""
    try:
        record = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


@cli.command(name="get")
@click.option(
    "--resource",
    type=click.Choice(sorted(n for n, s in RESOURCES.items() if s["id_path"])),
    required=True,
    help="Resource the id belongs to.",
)
@click.option("--id", "entity_id", required=True, help="Id to hydrate.")
@click.option(
    "--extension",
    default=None,
    help="Intelligence Card extension name (entity types only, e.g. 'links').",
)
def get(resource: str, entity_id: str, extension: str | None) -> None:
    """Hydrate a single entity/alert/playbook-alert/entity-match record by id."""
    spec = _resource(resource)
    if extension is not None:
        if not spec["extension_path"]:
            raise click.BadParameter(
                f"resource {resource!r} has no Intelligence Card extension",
                param_hint="--extension",
            )
        path = spec["extension_path"].format(id=entity_id, extension=extension)
    else:
        path = spec["id_path"].format(id=entity_id)

    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client, connector, method="GET", path=path, headers={"Accept": "application/json"}
        )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ioc",
            verb=f"get {resource}",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=[resource],
            client=client,
        )
    payload = json.loads(r.body)
    rows = _extract_rows(payload, spec["id_rows"])
    click.echo(json.dumps(rows[0] if rows else payload, indent=2))


@cli.command(name="riskrules")
@click.option(
    "--resource",
    type=click.Choice(sorted(n for n, s in RESOURCES.items() if s.get("riskrules_path"))),
    required=True,
    help="Entity type to list risk rules for.",
)
def riskrules(resource: str) -> None:
    """List the risk-rule catalog for one entity type (valid --param riskRule values)."""
    spec = _resource(resource)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=spec["riskrules_path"],
            headers={"Accept": "application/json"},
        )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ioc",
            verb=f"{resource} riskrules",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=[resource],
            client=client,
        )
    click.echo(r.body)


@cli.command(name="metadata")
@click.option(
    "--resource",
    type=click.Choice(sorted(METADATA_ENDPOINTS)),
    required=True,
    help="Which reference list to fetch.",
)
def metadata(resource: str) -> None:
    """Fetch a small fixed reference list (categories, statuses, link section types)."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=METADATA_ENDPOINTS[resource],
            headers={"Accept": "application/json"},
        )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ioc",
            verb=f"metadata {resource}",
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=[resource],
            client=client,
        )
    click.echo(r.body)


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a Recorded Future resource, in the canonical row
    shape upload-samples upserts: rows from `_search` (the same fetch
    `query` uses, no transform). A path that is not a single known resource
    returns []."""
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    resource = segments[0]
    params: dict[str, str] = {}
    if column is not None:
        if not values:
            return []
        params[column] = values[0] if len(values) == 1 else ",".join(values)
    return _search(client, connector, resource, params, limit)


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _RecordedFutureSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Recorded Future",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Recorded Future connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Recorded Future connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\nsmall unfiltered sample of each resource; resources that require filter\nfields to run at all (identity-password-lookup, links, entity-match) have\nno field tier since they cannot be sampled unfiltered.",
    refresh_docstring="Walk Recorded Future resource schema in real time and commit catalog tiers to the KG.\n\nThe only ``recordedfuture.py`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Recorded Future connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --param filters (as a JSON object) the samples came from -- must match the source query.",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `recordedfuture.py query --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /domain/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (JSON-encoded params),\nthe export cannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
