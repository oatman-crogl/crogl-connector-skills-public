#!/usr/bin/env python3
"""abuseipdb CLI -- enriches IP addresses via an AbuseIPDB community connector through the MCP api_proxy tool.

Subcommands:
  lookup          Look up one IP address; prints the full AbuseIPDB /check
                  record and caches it under a ref.
  view-result     Re-print a prior lookup's record by ref.
  upload-samples  Verify-and-upload a recent lookup to the knowledge graph.
  refresh         Commit the IOC-type catalog to the KG (shallow -- no field walk).
  ls              List this skill's AbuseIPDB connectors' database schema by UNIX path.
  grep            String-search this skill's connectors' KG catalog.

AbuseIPDB is an enrichment source: one IP in, one record out -- there is no
query language or searchable catalog. Auth is server-side: the agent container
provides CROGL_MCP_URL and CROGL_CLI_TOKEN. This skill is bound to one configured
connector and resolves it automatically; you don't pass a connector name. The
server attaches the connector's stored Key header when forwarding upstream.
"""

from __future__ import annotations

import ipaddress
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
    samples,
)


SKILL_NAME = "abuseipdb"
PACKAGE = "abuseipdb"
VIEW_TOOL_NAME = "abuseipdb.py view-result"

# AbuseIPDB enriches a single indicator kind: an IP address.
IOC_TYPE = "ip_address"
IOC_TYPES = (IOC_TYPE,)

DEFAULT_MAX_AGE_DAYS = 90


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _validate_ip(value: str) -> None:
    """Light client-side validation so an obviously-malformed address fails
    before a wasted upstream round-trip."""
    v = value.strip()
    if not v:
        raise click.BadParameter("value cannot be empty", param_hint="--value")
    try:
        ipaddress.ip_address(v)
    except ValueError as e:
        raise click.BadParameter(
            f"invalid IP address: {value!r}", param_hint="--value"
        ) from e


def _lookup(
    client: mcp.MCPClient, connector: str, value: str, max_age_days: int
) -> dict[str, Any]:
    """GET one IP's AbuseIPDB record and return its ``data`` object. AbuseIPDB
    /check takes the address and lookback window as query parameters."""
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path="/check",
        query={"ipAddress": value.strip(), "maxAgeInDays": str(max_age_days)},
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="ioc",
            verb="lookup",
            connector=connector,
            package=PACKAGE,
            query=value,
            response=r,
            candidates_path=None,
            client=client,
        )
    try:
        payload = json.loads(r.body)
    except ValueError as e:
        raise click.ClickException(
            f"lookup returned non-JSON body: {r.body[:500]}"
        ) from e
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise click.ClickException(
            f"unexpected AbuseIPDB response (no data object): {r.body[:300]}"
        )
    return data


def _query_id(value: str) -> str:
    """The provenance identity for a lookup: ``ip_address:<value>``. Passed back
    verbatim as --query to upload-samples for the read-before-write check."""
    return f"{IOC_TYPE}:{value.strip()}"


class _AbuseIPDBSchemaProvider:
    """SchemaProvider for AbuseIPDB: IOC-type -> (no fields).

    AbuseIPDB has no queryable schema -- the catalog is just the single
    ``ip_address`` object type. The leaf (field) tier is intentionally empty:
    per-record attributes are learned from uploaded ``lookup`` samples, not a
    crawl. Implements the contract in ``_lib/discover.py``.
    """

    PACKAGE = PACKAGE
    PLATFORM = "AbuseIPDB"
    TIERS_SINGULAR: tuple[str, ...] = ("IOCType", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("IOCTypes", "Fields")

    def __init__(self, client: mcp.MCPClient) -> None:
        self._client = client

    def list_my_connectors(self) -> list[str]:
        bound = instance.bound_connector_name(self._client)
        return [bound] if bound is not None else []

    def list_children(self, connector: str, tiers: list[str]) -> list[str]:
        return kgcommands.kg_list_children(self._client, connector, tiers)

    def discover_children(self, connector: str, tiers: list[str]) -> list[str]:
        """The IOC-type tier is a fixed set; there is no field tier to walk."""
        if len(tiers) == 0:
            return list(IOC_TYPES)
        return []


_EXAMPLES = """
Examples:

\b
  abuseipdb.py lookup --value 118.25.6.39
  abuseipdb.py lookup --value 8.8.8.8 --max-age-days 30
  abuseipdb.py view-result --ref <ref>
  abuseipdb.py grep --pattern ip_address
  abuseipdb.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, ip, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Enrich IP addresses via an AbuseIPDB community connector. Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


@cli.command(name="lookup")
@click.option("--value", required=True, help="The IPv4/IPv6 address to look up.")
@click.option(
    "--max-age-days",
    "max_age_days",
    type=click.IntRange(1, 365),
    default=DEFAULT_MAX_AGE_DAYS,
    show_default=True,
    help="Only consider reports from the last N days.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the record to FILE in the verifiable upload-samples format.",
)
@click.pass_context
def lookup(
    ctx: click.Context,
    value: str,
    max_age_days: int,
    output_path: str | None,
) -> None:
    """Look up one IP address and print its full AbuseIPDB record as JSON."""
    _validate_ip(value)
    query = _query_id(value)

    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        data = _lookup(client, connector, value, max_age_days)

    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/lookup"
    cache.put([[data]], ref=ref, producer=producer, connector=connector, query=query)

    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=query,
            ref=ref,
            rows=[data],
        )

    click.echo(json.dumps(data, indent=2))
    click.echo(f"Result reference: {ref}")
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        ioc_type=IOC_TYPE,
        ref=ref,
        output=output_path,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior lookup.")
def view_result(ref: str) -> None:
    """Re-print the full JSON record from a previously cached lookup."""
    try:
        record = cache.get_row(ref, 0, 0)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


kgcommands.add_commands(
    cli,
    lambda client: _AbuseIPDBSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="AbuseIPDB",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists AbuseIPDB connectors this skill owns; ``/<connector>/<ioc-type>`` drills into the IOC types. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's AbuseIPDB connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nThe catalog is shallow: ``/<connector>`` lists the single IOC type and\nthere is no field tier below it. Returns a JSON envelope (see\n``_lib/discover.py``).",
    refresh_docstring="Commit the AbuseIPDB IOC-type catalog to the KG.\n\nAbuseIPDB has no per-type field schema, so the walk is shallow: it\ncommits the single ip_address IOC type as the only catalog tier. ``ls``\nreads exclusively from the KG cache produced here.",
    grep_docstring="String-search this skill's AbuseIPDB connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph,\nnot the live API. Default match is a literal case-insensitive substring;\nuse --regex for a POSIX regex or --fuzzy for typo-tolerant trigram\nmatching. To search every connector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact lookup identity the sample came from (`ip_address:<value>`).",
    upload_ref_help="UUID of a recent lookup (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `abuseipdb.py lookup --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the record belongs to (e.g. /ip_address/<value>). Missing tiers are created server-side.",
    upload_docstring="Verify-then-upload the record from a recent lookup.\n\nSource is either `--ref UUID` (the cache from a recent `lookup`) or\n`--from-file FILE` (a file produced by `lookup --output`). Exactly one is\nrequired. Both paths enforce a read-before-write check: the agent must\nhave looked up this exact IP with this exact `--query`\n(`ip_address:<value>`), and a content hash must match.\n\nOn success the record is persisted via the hidden `upsert_data_sample`\nMCP tool; the server resolves --catalog-path tier-by-tier, lazily creating\nany missing catalog entries.",
    upload_mismatch_msg="query in --query does not match the lookup the record was fetched with (read-before-write check failed)",
    upload_limit_msg=lambda n: f"upload limit is {samples.MAX_UPLOAD_ROWS} rows, this cache holds {n}",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
