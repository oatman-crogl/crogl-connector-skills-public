#!/usr/bin/env python3
"""cisco_secure_access_ravpn CLI -- drives a Cisco Secure Access RA VPN
connector via the MCP api_proxy tool, covering the remote-access VPN
(AnyConnect / Secure Client) surface: historical connect/disconnect/failure
events (Reporting API) and currently active sessions (Admin API).

Subcommands:
  query           Fetch one resource (--resource), with arbitrary
                  --param KEY=VALUE filters and --limit. Emits grouped PSV
                  with a ref for drill-down.
  view-result     Re-print a prior query's cached rows.
  upload-samples  Verify-then-upload sampled rows to the knowledge graph.
  refresh         Walk each resource's catalog (resource -> field) into the KG.
  ls              List this skill's Secure Access connectors' schema by path.
  grep            String-search this skill's connectors' KG catalog.

Read-only: no session-disconnect (PUT) or any other write action is wired
into this connector, even though the underlying vpn-sessions endpoint
supports one.

Auth is server-side: the agent container provides CROGL_MCP_URL and
CROGL_CLI_TOKEN. This skill is bound to one configured connector and
resolves it automatically; you don't pass a connector name. The server
mints an OAuth2 client-credentials bearer token (HTTP Basic key:secret to
https://api.sse.cisco.com/auth/v2/token) and attaches it automatically.
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

SKILL_NAME = "cisco-secure-access-ravpn"
PACKAGE = "cisco_secure_access_ravpn"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "cisco_secure_access_ravpn.py view-result"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). Both resources are single-request
# (no pagination), so this bounds the one request's `limit` param directly.
# vpn-sessions documents its own vendor-side max of 1000; remote-access-events
# documents no max at all. HARD_CAP is this connector's own ceiling,
# independent of (and lower than) either.
DEFAULT_LIMIT = 25
HARD_CAP = 100

# Both resources live on the same host but under different versioned base
# paths (reports/v2 vs admin/v2), so each carries its own full path rather
# than a version baked into the connector's base_url -- same reasoning as
# Intune's per-resource Graph version.
RESOURCES: dict[str, dict[str, Any]] = {
    "remote-access-events": {
        "path": "/reports/v2/remote-access-events",
        "rows_key": "data",
        # from/to are mandatory per the API spec -- a bounded time window,
        # not an optional filter.
        "required_params": frozenset({"from", "to"}),
    },
    "vpn-sessions": {
        "path": "/admin/v2/vpn/userConnections",
        "rows_key": "data",
        "required_params": frozenset(),
    },
}
RESOURCE_NAMES = tuple(sorted(RESOURCES.keys()))


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


def _search(
    client: mcp.MCPClient,
    connector: str,
    resource: str,
    params: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    """Single-request fetch; `limit` is sent verbatim as the API's own `limit`
    query param -- there is no pagination to bound here, just the one
    request's size.

    Clamped to HARD_CAP regardless of caller (the CLI's own click.IntRange
    already enforces this for `query`, but this function is also reachable
    directly from the shared `sample`/`lookup` commands, whose own --limit
    is not IntRange-bounded).
    """
    limit = min(limit, HARD_CAP)
    spec = _resource(resource)
    missing = spec["required_params"] - params.keys()
    if missing:
        raise click.ClickException(
            f"resource {resource!r} requires --param for: {', '.join(sorted(missing))}"
        )
    query = {**params, "limit": str(limit)}
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=spec["path"],
        query=query,
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=f"{resource} query",
            connector=connector,
            package=PACKAGE,
            query=json.dumps(params, sort_keys=True),
            response=r,
            candidates_path=[resource],
            client=client,
        )
    payload = json.loads(r.body)
    data = payload.get(spec["rows_key"]) if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)][:limit]


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
    optionally write a verifiable upload-samples export, and print. A zero-row
    result prints an explicit line rather than a blank one, so it isn't
    mistaken for a silent failure."""
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


class _SecureAccessSchemaProvider:
    """SchemaProvider for Cisco Secure Access RA VPN: resource -> field.

    Neither resource exposes a createmeta-style field schema, so the leaf
    field tier is discovered empirically: remote-access-events needs a
    from/to window to sample at all (a recent 7-day window is used);
    vpn-sessions can be sampled unfiltered.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Cisco Secure Access RA VPN"
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
        params = (
            {"from": "-7days", "to": "now"}
            if resource == "remote-access-events"
            else {}
        )
        rows = _search(self._client, connector, resource, params, 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


_EXAMPLES = """
Examples:

\b
  cisco_secure_access_ravpn.py query --resource remote-access-events --param from=-1days --param to=now --limit 25
  cisco_secure_access_ravpn.py query --resource remote-access-events --param from=-1days --param to=now --param connectionevent=failed --limit 25
  cisco_secure_access_ravpn.py query --resource vpn-sessions --limit 25
  cisco_secure_access_ravpn.py query --resource vpn-sessions --param usernames=user@example.com --limit 5
  cisco_secure_access_ravpn.py view-result --ref <ref> --row 1
  cisco_secure_access_ravpn.py grep --pattern anyconnectversion
  cisco_secure_access_ravpn.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a Cisco Secure Access RA VPN connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(RESOURCE_NAMES),
    required=True,
    help="Secure Access RA VPN resource to fetch.",
)
@click.option(
    "--param",
    "params",
    multiple=True,
    help="Filter as KEY=VALUE; repeatable. See SKILL.md for each resource's fields "
    "-- remote-access-events requires from and to.",
)
@click.option(
    "--limit",
    required=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows to fetch (sent as the API's own `limit` query param; "
    f"hard cap {HARD_CAP}). No default -- {DEFAULT_LIMIT} is a reasonable "
    "starting point for an analyst-facing question; see SKILL.md.",
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
    """Fetch one resource and emit grouped PSV with a ref UUID for drill-down."""
    parsed = _parse_params(params)
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search(client, connector, resource, parsed, limit)
    query_id = json.dumps(parsed, sort_keys=True)
    _emit_rows(
        ctx,
        rows,
        command="query",
        connector=connector,
        query=query_id,
        catalog_path=[resource],
        started=started,
        output_path=output_path,
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
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query (pure cache read)."""
    try:
        record = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over a Secure Access RA VPN resource, in the canonical
    row shape upload-samples upserts: rows from `_search` (the same fetch
    `query` uses, no transform). remote-access-events needs a from/to window
    to sample at all; vpn-sessions can be sampled unfiltered."""
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []
    resource = segments[0]
    params: dict[str, str] = (
        {"from": "-7days", "to": "now"} if resource == "remote-access-events" else {}
    )
    if column is not None:
        if not values:
            return []
        params[column] = values[0] if len(values) == 1 else ",".join(values)
    return _search(client, connector, resource, params, limit)


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _SecureAccessSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Cisco Secure Access RA VPN",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Secure Access RA VPN connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Secure Access RA VPN connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a\nsmall sample of each resource (remote-access-events samples a 7-day\nwindow; vpn-sessions samples unfiltered).",
    refresh_docstring="Walk Secure Access RA VPN resource schema in real time and commit catalog tiers to the KG.\n\nThe only ``cisco_secure_access_ravpn.py`` command that hits the connector\nbackend for schema discovery; ``ls`` reads exclusively from the KG cache.\nWalks are resumable per-tier; stale schema items are pruned at the tier\nthey disappear from.",
    grep_docstring="String-search this skill's Secure Access RA VPN connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --param filters (as a JSON object) the samples came from -- must match the source query.",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `cisco_secure_access_ravpn.py query --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /vpn-sessions/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent query) or\n`--from-file FILE` (a file produced by `query --output`). Exactly one is\nrequired. Both enforce a read-before-write check: the agent must have\nqueried these exact rows with this exact `--query` (JSON-encoded params),\nthe export cannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
