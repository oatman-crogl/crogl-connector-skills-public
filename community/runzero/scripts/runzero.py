#!/usr/bin/env python3
"""runzero CLI -- drives a runZero community connector through the MCP
api_proxy tool, covering the read-only Export API (an Export/`ET` token can
reach only paths under /api/v1.0/export -- it structurally cannot write).

Subcommands:
  query           Search one resource (--resource) with runZero's own
                  search-query syntax (--search), optional --fields, and
                  --limit. Emits grouped PSV with a ref for drill-down.
  get             Hydrate one row by id (assets, vulnerabilities, wireless,
                  certificates only -- the only resources with a documented
                  `id` search keyword).
  view-result     Re-print one cached row from a prior query.
  refresh, ls, grep, upload-samples   Standard KG catalog/sample commands.

Explicitly out of scope (a separate, higher-privilege connector type, not
wired into RESOURCES below): every write/CRUD path under /org/* (create or
edit sites, assets, scans -- requires an Organization `OT` token) and every
platform-admin path under /account/* (orgs, users, keys -- requires an
Account `CT` token). Both are different auth models and a different trust
boundary from read-only export, and were deliberately left out of this
connector's scope -- see SKILL.md.

Auth is server-side: the agent container provides CROGL_MCP_URL and
CROGL_CLI_TOKEN. This skill is bound to one configured connector and
resolves it automatically; you don't pass a connector name. The server
attaches the connector's stored Export Token as the `Authorization: Bearer`
header when forwarding upstream.
"""

from __future__ import annotations

import csv
import io
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

SKILL_NAME = "runzero"
PACKAGE = "runzero"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "runzero.py view-result"

# Context-budget rules (mirror the SKILL.md). A query defaults to 5 rows;
# even an explicit "all"/"everything" request may not exceed the hard cap --
# runZero's own daily rate limit is tied to licensed asset count, so wide
# unbounded pulls are expensive against the tenant's own quota, not just slow.
DEFAULT_LIMIT = 5
HARD_CAP = 25

# Every entry is a path under /export/org/ on an Export (`ET`) token. Verified
# against runZero's published OpenAPI spec (github.com/runZeroInc/runzero-api)
# -- not every export resource supports every query param, and two (the CSV-
# only stats endpoints) have no JSON form at all:
#   search       -- the `search` query param (runZero search-query syntax)
#   fields       -- the `fields` query param (comma-separated field allowlist)
#   page_size    -- the `page_size` query param; when set the response is a
#                   {resource: [...], next_key} object instead of a bare
#                   array, which lets the CLI bound the response server-side.
#                   Resources without this support return their FULL match
#                   set every time -- there is no server-side cap, so a
#                   narrow --search matters more on those.
#
# CONTRIBUTING.md rule 4.4 ("every list-returning request must send a
# server-side limiting param") is satisfied where a param exists (the five
# `page_size` resources above) and cannot be satisfied for the other eight --
# re-verified 2026-09-10 against runZero's published OpenAPI spec
# (github.com/runZeroInc/runzero-api): sites/certificates/users/groups/
# findings/tasks accept no page_size, limit, per_page, or any other
# row-count param on their export endpoints at all (confirmed field-by-field
# in the spec, not just the vendor's prose docs); subnet-utilization is a
# small bounded per-subnet aggregate, not a growing per-row table, so the
# absence of a limit param there is low-risk; snmp-arpcache is the one
# resource with a real residual risk (no search, no fields, no size param of
# any kind) and no available mitigation on the request side -- HARD_CAP still
# bounds what this CLI caches/renders after the fetch (see rows[:...] below),
# which is the only lever available when the vendor exposes none.
#   csv          -- endpoint has no JSON export; only .csv. Parsed with
#                   csv.DictReader instead of json.loads.
#   array_key    -- for page_size resources, the object key holding the row
#                   array (confirmed from the OpenAPI Page schemas, not the
#                   prose descriptions -- one of those has a typo).
RESOURCES: dict[str, dict[str, Any]] = {
    "assets": {"path": "assets.json", "search": True, "fields": True, "page_size": True, "array_key": "assets"},
    "services": {"path": "services.json", "search": True, "fields": True, "page_size": True, "array_key": "services"},
    "software": {"path": "software.json", "search": True, "fields": True, "page_size": True, "array_key": "software"},
    "vulnerabilities": {"path": "vulnerabilities.json", "search": True, "fields": True, "page_size": True, "array_key": "vulnerabilities"},
    "wireless": {"path": "wireless.json", "search": True, "fields": True, "page_size": True, "array_key": "wireless"},
    "sites": {"path": "sites.json", "search": True, "fields": True, "page_size": False},
    "certificates": {"path": "certificates.json", "search": True, "fields": False, "page_size": False},
    "users": {"path": "users.json", "search": True, "fields": True, "page_size": False},
    "groups": {"path": "groups.json", "search": True, "fields": True, "page_size": False},
    "findings": {"path": "findings.json", "search": True, "fields": False, "page_size": False},
    "tasks": {"path": "tasks.json", "search": True, "fields": True, "page_size": False},
    "subnet-utilization": {"path": "subnet.stats.csv", "search": False, "fields": False, "page_size": False, "csv": True, "mask_param": True},
    "snmp-arpcache": {"path": "snmp.arpcache.csv", "search": False, "fields": False, "page_size": False, "csv": True},
}
RESOURCE_NAMES = tuple(sorted(RESOURCES))

# Resources with a documented `id` search keyword -- the only ones `get`
# supports. (services/software/sites/users/groups/findings/tasks either lack
# a confirmed `id` keyword in runZero's docs or use a different identity
# field entirely -- e.g. findings key off `finding_code`, not `id`. Query
# those with the appropriate --search field instead.)
GET_ID_RESOURCES = ("assets", "vulnerabilities", "wireless", "certificates")


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


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _parse_csv(body: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(body))
    return [dict(row) for row in reader]


def _fetch_resource(
    client: mcp.MCPClient,
    connector: str,
    resource: str,
    *,
    search: str,
    fields: str,
    mask: str,
    limit: int,
    verb: str,
) -> list[dict[str, Any]]:
    spec = _resource(resource)
    query: dict[str, str] = {}

    if search:
        if not spec["search"]:
            raise click.ClickException(
                f"{resource!r} has no `search` query param on its export endpoint "
                "-- it always returns its full (unfiltered) result set."
            )
        query["search"] = search
    if fields:
        if not spec["fields"]:
            raise click.ClickException(f"{resource!r} does not support --fields.")
        query["fields"] = fields
    if mask:
        if not spec.get("mask_param"):
            raise click.ClickException(f"{resource!r} does not support --mask.")
        query["mask"] = mask
    if spec["page_size"]:
        query["page_size"] = str(min(limit, HARD_CAP))

    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=f"/export/org/{spec['path']}",
        query=query,
        headers={"Accept": "text/csv" if spec.get("csv") else "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=search or None,
            response=r,
            candidates_path=[resource],
            client=client,
        )

    if spec.get("csv"):
        rows = _parse_csv(r.body)
    else:
        payload = _parse_json(r.body, verb)
        if isinstance(payload, list):
            arr = payload
        elif isinstance(payload, dict) and spec.get("array_key"):
            arr = payload.get(spec["array_key"], [])
        else:
            raise click.ClickException(f"{verb}: unexpected response shape: {type(payload).__name__}")
        rows = [row for row in arr if isinstance(row, dict)]

    return rows[: min(limit, HARD_CAP)]


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
    """Marshal rows to grouped PSV, cache them under a fresh ref for drill-down,
    optionally write a verifiable upload-samples export, and print. Zero
    matches still print an explicit line -- never a blank line -- so the
    agent can tell "query succeeded, nothing matched" from a silent failure."""
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


class _RunZeroSchemaProvider:
    """SchemaProvider for runZero: resource -> field.

    Every resource's field tier is discovered empirically from a one-row live
    sample (`page_size=1`/full-fetch-then-truncate as `_fetch_resource`
    already does) -- runZero does not publish a stable per-resource field
    schema the way QRadar's Ariel datasets do. Implements the contract in
    ``_lib/discover.py``.
    """

    PACKAGE = PACKAGE
    PLATFORM = "runZero"
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
        rows = _fetch_resource(
            self._client,
            connector,
            resource,
            search="",
            fields="",
            mask="",
            limit=1,
            verb=f"sample {resource}",
        )
        if rows:
            return sorted(str(k) for k in rows[0].keys())
        return []


_EXAMPLES = """
Examples:

\b
  runzero.py query --resource assets --search "os:\\"Windows\\" AND alive:true" --limit 5
  runzero.py query --resource vulnerabilities --search "cve:CVE-2021-44228" --limit 10
  runzero.py query --resource software --search "product:log4j" --limit 10
  runzero.py get --resource assets --id <uuid>
  runzero.py query --resource subnet-utilization --mask 24
  runzero.py view-result --ref <ref> --row 1
  runzero.py grep --pattern os_eol
  runzero.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a runZero connector (read-only Export API). Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option("--resource", type=click.Choice(RESOURCE_NAMES), required=True, help="runZero export resource to search.")
@click.option(
    "--search",
    "search_expr",
    default="",
    help='runZero search-query syntax, e.g. \'os:"Windows" AND alive:true\'. '
    "Not supported on subnet-utilization/snmp-arpcache (CSV-only, no filter).",
)
@click.option("--fields", default="", help="Comma-separated fields to return. Not all resources support this -- see SKILL.md.")
@click.option("--mask", default="", help="Subnet mask size (e.g. 24) -- subnet-utilization only.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write rows to FILE in the upload-samples format.")
@click.pass_context
def query(ctx: click.Context, resource: str, search_expr: str, fields: str, mask: str, limit: int, output_path: str | None) -> None:
    """Search one resource; emits grouped PSV with a ref for drill-down."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _fetch_resource(client, connector, resource, search=search_expr, fields=fields, mask=mask, limit=limit, verb=f"query {resource}")
    _emit_rows(ctx, rows, command=f"query:{resource}", connector=connector, query=search_expr, catalog_path=[resource], started=started, output_path=output_path)


@cli.command(name="get")
@click.option("--resource", type=click.Choice(GET_ID_RESOURCES), required=True, help="Resource to hydrate by id.")
@click.option("--id", "id_value", required=True, help="The resource's `id` field (UUID).")
def get(resource: str, id_value: str) -> None:
    """Hydrate one row by id and print the full JSON detail card."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _fetch_resource(
            client, connector, resource,
            search=f'id:{id_value}', fields="", mask="", limit=1, verb=f"get {resource}",
        )
    if not rows:
        raise click.ClickException(f"no {resource} row with id {id_value!r}")
    click.echo(json.dumps(rows[0], indent=2))


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="Row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query (pure cache read)."""
    try:
        record = cache.get_row(ref, result_set, row - 1)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a runZero resource. When a column/values pair is
    given, filter with an OR chain of exact-match equalities (`field:="value"`
    -- the `=` prefix disables runZero's default fuzzy match so identity
    lookups don't over-match). Resources without --search support (the two
    CSV stats endpoints) return their full page every time regardless."""
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    resource = segments[0]
    search = ""
    if column is not None and values:
        search = " OR ".join(f'{column}:={_quote(v)}' for v in values)
    return _fetch_resource(client, connector, resource, search=search, fields="", mask="", limit=limit, verb=f"sample {resource}")


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _RunZeroSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="runZero",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists runZero connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's runZero connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\nsmall unfiltered live sample of each resource.",
    refresh_docstring="Walk runZero resource/field schema and commit catalog tiers to the KG.\n\nThe only ``runzero.py`` command that hits the connector backend for schema\ndiscovery; ``ls`` reads exclusively from the KG cache. Each resource is\nsampled a row at a time (fields unioned across samples).",
    grep_docstring="String-search this skill's runZero connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --search the samples came from (must match the source search; pass an empty string if the search used none).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by a `runzero.py query --output ...` run.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /assets/<field> or /vulnerabilities/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent search) or\n`--from-file FILE` (a file produced by `--output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query`, the export cannot\nexceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
    upload_mismatch_msg="query in --query does not match the search the rows were fetched with (read-before-write check failed)",
    upload_limit_msg=lambda n: f"upload limit is {samples.MAX_UPLOAD_ROWS} rows, this cache holds {n}",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
