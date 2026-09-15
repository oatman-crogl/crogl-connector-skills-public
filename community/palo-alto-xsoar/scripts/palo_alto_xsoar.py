#!/usr/bin/env python3
"""palo-alto-xsoar CLI -- drives a Palo Alto Cortex XSOAR 8 custom connector
via the MCP api_proxy tool (``/xsoar/public/v1`` REST surface).

Subcommands:
  query                Search incidents by XSOAR's own field:value query
                        syntax (or a small set of structured filters when
                        --query is omitted). Emits grouped PSV with a ref for
                        drill-down.
  get-incident          Hydrate one incident by numeric id -- full detail card.
  list-incident-fields  List all incident field definitions in the tenant
                        (GET /incidentfields) -- use this to resolve
                        instance-specific custom field names.
  view-result           Re-print one row (full entity JSON) from a prior
                        query.
  upload-samples        Verify-then-upload sampled incidents to the
                        knowledge graph.
  refresh                Walk the incidents/field catalog into the KG.
  ls                     List this skill's XSOAR connectors' schema by UNIX
                        path.
  grep                   String-search this skill's connectors' KG catalog.

Auth is a Standard-type Cortex XSOAR 8 API key: the Key ID is injected
server-side as the ``x-xdr-auth-id`` header, the key value as the raw
``Authorization`` header value, both from the connector's stored secrets.
Advanced-type keys (which require a per-request SHA256(api_key+nonce+
timestamp) signature) are NOT supported by this connector -- a static header
binding cannot compute a per-request hash. This skill is bound to one
configured connector and resolves it automatically; you don't pass a
connector name.

Read-only: no create/update/close/delete actions are exposed.
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

SKILL_NAME = "palo-alto-xsoar"
PACKAGE = "palo-alto-xsoar"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "palo_alto_xsoar.py view-result"

# Context-budget rules (mirror the SKILL.md). A query call defaults to 10
# rows; even an explicit "all"/"everything" request may not exceed the hard
# cap. The vendor API itself allows size up to 10000 -- HARD_CAP is this
# connector's own, tighter budget guard, not a vendor limit.
DEFAULT_LIMIT = 10
HARD_CAP = 50

INCIDENTS_SEARCH_PATH = "/incidents/search"
INCIDENT_LOAD_PATH = "/incident/load"
INCIDENT_FIELDS_PATH = "/incidentfields"

# Single resource in scope for this connector (read-only incident search).
RESOURCE_NAME = "incidents"

_JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _build_filter(
    query: str,
    category: tuple[str, ...],
    type_: tuple[str, ...],
    from_date: str,
    to_date: str,
    sort_field: str,
    sort_desc: bool,
    limit: int,
) -> dict[str, Any]:
    """Build the ``filter`` object for POST /incidents/search.

    Per the vendor docs, setting ``query`` causes the API to ignore every
    other filter field -- so when --query is given this connector sends
    *only* query + size, matching that documented behavior rather than
    guessing whether pagination/sort survive alongside it (unverified,
    no live tenant to confirm -- see SKILL.md).
    """
    size = min(limit, HARD_CAP)
    if query:
        return {"query": query, "size": size, "page": 0}

    filt: dict[str, Any] = {"size": size, "page": 0}
    if category:
        filt["category"] = list(category)
    if type_:
        filt["type"] = list(type_)
    if from_date:
        filt["fromDate"] = from_date
    if to_date:
        filt["toDate"] = to_date
    if sort_field:
        filt["sort"] = [{"field": sort_field, "asc": not sort_desc}]
    return filt


def _search_incidents(
    client: mcp.MCPClient,
    connector: str,
    query: str,
    category: tuple[str, ...],
    type_: tuple[str, ...],
    from_date: str,
    to_date: str,
    sort_field: str,
    sort_desc: bool,
    limit: int,
) -> list[dict[str, Any]]:
    """POST /incidents/search. The response is an object
    ``{data: [...], total: N}`` -- unwrap ``data``."""
    filt = _build_filter(
        query, category, type_, from_date, to_date, sort_field, sort_desc, limit
    )
    query_text = query or json.dumps(filt)
    r = proxy.dispatch(
        client,
        connector,
        method="POST",
        path=INCIDENTS_SEARCH_PATH,
        body=json.dumps({"filter": filt}),
        headers=_JSON_HEADERS,
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="xsoar-search",
            verb="search incidents",
            connector=connector,
            package=PACKAGE,
            query=query_text,
            response=r,
            candidates_path=[RESOURCE_NAME],
            client=client,
        )
    payload = _parse_json(r.body, "search incidents")
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"search incidents: expected an object with a 'data' array, got "
            f"{type(payload).__name__}"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise click.ClickException(
            "search incidents: expected payload to have a 'data' array"
        )
    return [row for row in data if isinstance(row, dict)][:limit]


def _emit_rows(
    rows: list[dict[str, Any]],
    *,
    command: str,
    connector: str,
    query: str,
    catalog_path: list[str] | None,
) -> str | None:
    """Marshal rows to grouped PSV, cache them under a fresh ref for
    drill-down, and print. Returns the ref (or None for an empty result).
    An empty result prints an explicit 0-row line (never a blank line) so
    the agent doesn't mistake success-with-no-data for a failed call and
    retry."""
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/{command}"

    if not rows:
        query_note = f" for {query!r}" if query else ""
        click.echo(f"No {command} results (0 rows){query_note}.")
        return None

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
    click.echo(body_text)
    return ref


class _XsoarSchemaProvider:
    """SchemaProvider for palo-alto-xsoar: incidents -> field.

    Unlike Intune's Graph surface, XSOAR has a real schema endpoint
    (``GET /incidentfields``), so field discovery is authoritative rather
    than sampled from one row -- stale fields are pruned at the tier they
    disappear from.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Cortex XSOAR"
    TIERS_SINGULAR: tuple[str, ...] = ("Resource", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Resources", "Fields")
    LEAF_DISCOVERY_SAMPLED = False

    def __init__(self, client: mcp.MCPClient) -> None:
        self._client = client

    def list_my_connectors(self) -> list[str]:
        bound = instance.bound_connector_name(self._client)
        return [bound] if bound is not None else []

    def list_children(self, connector: str, tiers: list[str]) -> list[str]:
        return kgcommands.kg_list_children(self._client, connector, tiers)

    def discover_children(self, connector: str, tiers: list[str]) -> list[str]:
        if len(tiers) == 0:
            return [RESOURCE_NAME]
        if len(tiers) == 1 and tiers[0] == RESOURCE_NAME:
            return self._discover_fields(connector)
        return []

    def _discover_fields(self, connector: str) -> list[str]:
        r = proxy.dispatch(
            self._client,
            connector,
            method="GET",
            path=INCIDENT_FIELDS_PATH,
            headers={"Accept": "application/json"},
        )
        if r.status_code >= 400:
            return []
        payload = _parse_json(r.body, "list incident fields")
        if not isinstance(payload, list):
            return []
        names: list[str] = []
        for row in payload:
            if isinstance(row, dict) and isinstance(row.get("cliName"), str):
                names.append(row["cliName"])
        return sorted(set(names))


_EXAMPLES = """
Examples:

\b
  palo_alto_xsoar.py query --query "-status:closed -category:job" --limit 10
  palo_alto_xsoar.py query --type Phishing --from-date "2024-01-01T00:00:00Z" --limit 25
  palo_alto_xsoar.py get-incident --id 162669
  palo_alto_xsoar.py list-incident-fields
  palo_alto_xsoar.py view-result --ref <ref> --row 1
  palo_alto_xsoar.py grep --pattern owner
  palo_alto_xsoar.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Drive a Palo Alto Cortex XSOAR 8 connector. Run `<command> --help` for details."""
    ctx.obj = {}


@cli.command(name="query")
@click.option(
    "--query",
    "query_text",
    default="",
    help="Raw XSOAR incident search query, e.g. \"-status:closed -category:job "
    "type:Phishing\" (colon is equality, leading '-' excludes). Setting this "
    "causes the API to ignore --category/--type/--from-date/--to-date "
    "(vendor-documented behavior). See SKILL.md 'Query syntax'.",
)
@click.option(
    "--category",
    multiple=True,
    help="Incident category to include (repeatable). Ignored if --query is set.",
)
@click.option(
    "--type",
    "type_",
    multiple=True,
    help="Incident type name to include (repeatable). Ignored if --query is set.",
)
@click.option(
    "--from-date",
    default="",
    help="ISO 8601 UTC lower bound on incident creation time, e.g. "
    "2024-01-01T00:00:00Z. Ignored if --query is set.",
)
@click.option(
    "--to-date",
    default="",
    help="ISO 8601 UTC upper bound on incident creation time. Ignored if --query is set.",
)
@click.option(
    "--sort-field",
    default="created",
    show_default=True,
    help="Field to sort by (e.g. created, modified, severity). Ignored if --query is set.",
)
@click.option(
    "--sort-desc/--sort-asc",
    default=True,
    help="Sort direction (default: newest/highest first). Ignored if --query is set.",
)
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows (hard cap {HARD_CAP}; the vendor API itself allows up to 10000).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def query(
    query_text: str,
    category: tuple[str, ...],
    type_: tuple[str, ...],
    from_date: str,
    to_date: str,
    sort_field: str,
    sort_desc: bool,
    limit: int,
    output_path: str | None,
) -> None:
    """Search incidents; emits grouped PSV with a ref for drill-down."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_incidents(
            client,
            connector,
            query_text,
            category,
            type_,
            from_date,
            to_date,
            sort_field,
            sort_desc,
            limit,
        )
    ref = _emit_rows(
        rows,
        command="query",
        connector=connector,
        query=query_text,
        catalog_path=[RESOURCE_NAME],
    )
    if output_path and rows and ref is not None:
        samples.write_export(
            output_path,
            producer=f"{SKILL_NAME}/query",
            connector=connector,
            query=query_text,
            ref=ref,
            rows=rows,
        )


@cli.command(name="get-incident")
@click.option("--id", "incident_id", required=True, help="Numeric incident id, e.g. 162669.")
def get_incident(incident_id: str) -> None:
    """Hydrate one incident by id and print the full JSON detail card."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=f"{INCIDENT_LOAD_PATH}/{incident_id}",
            headers={"Accept": "application/json"},
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="rest-key",
                verb="get incident",
                connector=connector,
                package=PACKAGE,
                query=incident_id,
                response=r,
                candidates_path=None,
                client=client,
            )
        incident = _parse_json(r.body, "get incident")
    click.echo(json.dumps(incident, indent=2))


@cli.command(name="list-incident-fields")
def list_incident_fields() -> None:
    """List all incident field definitions in the tenant.

    GET /incidentfields returns a bare JSON array; printed as-is so the agent
    can map a human field name (or a custom field's cliName) to what the
    query syntax and CustomFields object expect on this instance."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=INCIDENT_FIELDS_PATH,
            headers={"Accept": "application/json"},
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="rest-key",
                verb="list incident fields",
                connector=connector,
                package=PACKAGE,
                response=r,
                candidates_path=None,
                client=client,
            )
        payload = _parse_json(r.body, "list incident fields")
    click.echo(json.dumps(payload, indent=2))


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


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """KG sample/lookup over incidents, in the canonical row shape
    upload-samples upserts: incident objects from `_search_incidents` (the
    same fetch `query` uses, no transform). A path that is not the single
    known resource returns []."""
    if len(segments) != 1 or segments[0] != RESOURCE_NAME:
        return []
    if column is not None:
        if not values:
            return []
        query_text = " ".join(f"{column}:'{v}'" for v in values)
    else:
        query_text = ""
    return _search_incidents(
        client, connector, query_text, (), (), "", "", "created", True, limit
    )


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _XsoarSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Cortex XSOAR",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists XSOAR connectors this skill owns; ``/<connector>/incidents/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Cortex XSOAR connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: incidents -> Field. Fields are discovered from GET /incidentfields (cliName), not sampled.",
    refresh_docstring="Walk the incidents field catalog in real time and commit catalog tiers to the KG.\n\nThe only ``palo-alto-xsoar`` command that hits the connector backend for\nschema discovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from.",
    grep_docstring="String-search this skill's Cortex XSOAR connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact --query (or, if empty, the --category/--type used) the samples came from -- must match the source search.",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `palo_alto_xsoar.py query --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /incidents/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE`. Exactly one is required. Both enforce a read-before-write\ncheck: the agent must have queried these exact rows with this exact\n`--query`, the export cannot exceed the upload limit, and a content hash\nmust match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
