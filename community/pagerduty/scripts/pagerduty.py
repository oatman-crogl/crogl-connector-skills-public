#!/usr/bin/env python3
"""pagerduty CLI -- drives a PagerDuty connector via the MCP api_proxy tool.

Scope: PagerDuty's REST API v2 incident lifecycle -- creating/escalating an
incident, acknowledging/resolving/reassigning one, commenting on one, and the
read-only lookups (services, escalation policies, priorities, on-calls,
incident search/get) an agent needs to pick a correct target before writing.

Deliberately NOT covered -- see SKILL.md's Scope section for why:
  * PagerDuty's Events API v2 (raw monitoring-tool event ingestion via a
    per-service routing key). PagerDuty's own API docs recommend this REST
    API's synchronous Incidents API instead for a caller that is itself a
    system of record making an analyzed decision to escalate, which is what
    this connector's callers are.
  * Schedules, users/teams CRUD, maintenance windows, webhooks/extensions,
    and any other REST API v2 resource outside the incident lifecycle above.

Auth is a REST API v2 key, injected server-side as Authorization: Token
token=<key>, plus a From header carrying a PagerDuty user's email -- required
by PagerDuty on every write call (create/update an incident, add a note) and
ignored on reads. Both are stored as connector credential fields; see
SKILL.md's Auth Recap for why From is marked secret even though it isn't one.
This skill is bound to one configured connector and resolves it
automatically; you don't pass a connector name.

WRITE POSTURE (see SKILL.md's Guardrails for the full rationale):
  * create-incident fires immediately, with NO --yes gate. This is a
    deliberate, scope-approved exception to this repo's usual write-gate
    convention: the whole point of this connector is unattended escalation
    to PagerDuty when a customer has no 24/7 SOC watching, so a human
    confirmation step would defeat the purpose. --incident-key is REQUIRED
    (not optional, unlike the vendor's own field) so a retried/duplicate
    invocation for the same correlated detection can't fan out extra pages.
  * update-incident and add-comment (acknowledge/resolve/reassign/
    reprioritize an EXISTING incident, or comment on one) are gated behind
    --yes like every other write connector in this repo -- these are not the
    autonomous-escalation path and deserve the normal approval protocol.
"""

from __future__ import annotations

import json
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

SKILL_NAME = "pagerduty"
PACKAGE = "pagerduty"
PLATFORM_LABEL = "PagerDuty"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "pagerduty.py view-result"
# PagerDuty's own documented per-page maximum is 100 (GET /incidents `limit`);
# use it directly as the hard cap rather than looping across pages -- narrow
# with filters instead of paginating a large result into context.
DEFAULT_LIMIT = 25
HARD_CAP = 100

RESOURCE_NAMES = ("incidents",)

SERVICES_PATH = "/services"
SERVICE_PATH = "/services/{id}"
ESCALATION_POLICIES_PATH = "/escalation_policies"
ESCALATION_POLICY_PATH = "/escalation_policies/{id}"
PRIORITIES_PATH = "/priorities"
ONCALLS_PATH = "/oncalls"
INCIDENTS_PATH = "/incidents"
INCIDENT_PATH = "/incidents/{id}"
INCIDENT_NOTES_PATH = "/incidents/{id}/notes"

INCIDENT_STATUS_VALUES = ("triggered", "acknowledged", "resolved")
URGENCY_VALUES = ("high", "low")


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _headers() -> dict[str, str]:
    """Per-call headers. Authorization and From are injected server-side; the
    CLI only asks for JSON."""
    return {"Accept": "application/json"}


def _write_headers() -> dict[str, str]:
    """Headers for a request that carries a JSON body."""
    return {"Accept": "application/json", "Content-Type": "application/json"}


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _query_desc(command: str, **kwargs: Any) -> str:
    return json.dumps({"command": command, **kwargs}, sort_keys=True, separators=(",", ":"))


def _get_json(
    client: mcp.MCPClient,
    connector: str,
    *,
    path: str,
    verb: str,
    query_desc: str | None,
    query: dict[str, str] | None,
    candidates_path: list[str] | None,
) -> Any:
    """GET a PagerDuty REST v2 endpoint and return the decoded JSON payload.

    PagerDuty's incident search/filters are flat named REST params, not a
    query language -- there is no dedicated `queryerror.GRAMMAR`/`EXAMPLE`
    entry for this connector, so every call here uses the `"querying"`
    fallback interface, which omits a grammar hint rather than rendering a
    wrong one borrowed from an unrelated connector."""
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query or {},
        headers=_headers(),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_desc,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    return _parse_json(r.body, verb)


def _require_approval(yes: bool, intent: str) -> None:
    """Approval gate for update-incident/add-comment.

    Raised BEFORE any MCP/PagerDuty call, so a gated command has no side
    effect at all -- it only reports the mutation it would perform. The agent
    must relay that line to the user, get an explicit yes, and only then
    re-run with --yes. Does NOT apply to create-incident, which fires
    unconditionally by design -- see the module docstring's Write Posture."""
    if not yes:
        raise click.ClickException(
            f"WRITE gated: would {intent}. Nothing was sent to PagerDuty. Show "
            "this to the user and re-run with --yes only after they explicitly "
            "approve this exact action."
        )


def _write(
    client: mcp.MCPClient,
    connector: str,
    *,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    verb: str,
    query_text: str | None,
) -> proxy.ProxyResponse:
    """Send one mutating request and return the raw response.

    Never retried: a re-sent POST would double the side effect upstream (page
    someone twice, double-post a note)."""
    r = proxy.dispatch(
        client,
        connector,
        method=method,
        path=path,
        headers=_write_headers(),
        body=json.dumps(payload) if payload is not None else None,
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="querying",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_text,
            response=r,
            candidates_path=None,
            client=client,
        )
    return r


def _emit_write_result(r: proxy.ProxyResponse, *, verb: str, done: str) -> None:
    """Print a write's outcome. An empty body is success, not a parse failure."""
    body = r.body.strip()
    if not body:
        click.echo(done)
        return
    try:
        click.echo(json.dumps(json.loads(body), indent=2))
    except ValueError:
        click.echo(f"{verb}: {r.status_code} {body[:500]}")


def _emit_rows(
    rows: list[dict[str, Any]],
    *,
    command: str,
    connector: str,
    query_desc: str,
    catalog_path: list[str] | None,
) -> str | None:
    """Marshal rows to grouped PSV, cache them under a fresh ref for drill-down,
    and print. An empty result prints an explicit 0-row line (never a blank
    line) so the agent doesn't mistake success-with-no-data for a failed call
    and retry. Returns the cache ref (or None for an empty result) so callers
    can conditionally write --output."""
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/{command}"

    if not rows:
        click.echo(f"No {command} results (0 rows).")
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
        query=query_desc,
        catalog_path=catalog_path,
    )
    click.echo(body_text)
    return ref


def _list_ids(values: tuple[str, ...] | None) -> list[str]:
    return list(values) if values else []


def _fetch_reference(
    client: mcp.MCPClient,
    connector: str,
    *,
    path: str,
    kind: str,
    value: str,
) -> dict[str, Any]:
    """Resolve a --service/--escalation-policy id by fetching it directly
    (GET .../{id}), never by loose name matching. A caller must pass the real
    id from list-services/list-escalation-policies; a wrong or stale id fails
    here with a clear "run list-X first" message instead of silently routing
    to whatever a fuzzy match happened to pick -- see SKILL.md's Guardrails."""
    payload = _get_json(
        client,
        connector,
        path=path.format(id=value),
        verb=f"get {kind}",
        query_desc=value,
        query=None,
        candidates_path=None,
    )
    envelope_key = {
        SERVICE_PATH: "service",
        ESCALATION_POLICY_PATH: "escalation_policy",
    }[path]
    ref = payload.get(envelope_key) if isinstance(payload, dict) else None
    if not isinstance(ref, dict):
        raise click.ClickException(
            f"get {kind}: expected an object with a {envelope_key!r} key, got "
            f"{type(payload).__name__}"
        )
    return ref


# --- lookup listings (services / escalation policies / priorities / oncalls) ---


def _list_services(
    client: mcp.MCPClient,
    connector: str,
    *,
    query: str | None,
    name: str | None,
    team_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": str(min(limit, HARD_CAP))}
    if query:
        params["query"] = query
    if name:
        params["name"] = name
    for i, t in enumerate(team_ids):
        params[f"team_ids[{i}]"] = t
    payload = _get_json(
        client,
        connector,
        path=SERVICES_PATH,
        verb="list services",
        query_desc=json.dumps(params, sort_keys=True),
        query=params,
        candidates_path=None,
    )
    rows = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise click.ClickException(
            f"list services: expected an object with a 'services' array, got "
            f"{type(payload).__name__}"
        )
    return [row for row in rows if isinstance(row, dict)][:limit]


def _list_escalation_policies(
    client: mcp.MCPClient,
    connector: str,
    *,
    query: str | None,
    user_ids: list[str],
    team_ids: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": str(min(limit, HARD_CAP))}
    if query:
        params["query"] = query
    for i, u in enumerate(user_ids):
        params[f"user_ids[{i}]"] = u
    for i, t in enumerate(team_ids):
        params[f"team_ids[{i}]"] = t
    payload = _get_json(
        client,
        connector,
        path=ESCALATION_POLICIES_PATH,
        verb="list escalation policies",
        query_desc=json.dumps(params, sort_keys=True),
        query=params,
        candidates_path=None,
    )
    rows = payload.get("escalation_policies") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise click.ClickException(
            "list escalation policies: expected an object with an "
            f"'escalation_policies' array, got {type(payload).__name__}"
        )
    return [row for row in rows if isinstance(row, dict)][:limit]


def _list_priorities(
    client: mcp.MCPClient, connector: str, *, limit: int
) -> list[dict[str, Any]]:
    params = {"limit": str(min(limit, HARD_CAP))}
    payload = _get_json(
        client,
        connector,
        path=PRIORITIES_PATH,
        verb="list priorities",
        query_desc=None,
        query=params,
        candidates_path=None,
    )
    rows = payload.get("priorities") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise click.ClickException(
            f"list priorities: expected an object with a 'priorities' array, "
            f"got {type(payload).__name__}"
        )
    return [row for row in rows if isinstance(row, dict)][:limit]


def _list_oncalls(
    client: mcp.MCPClient,
    connector: str,
    *,
    escalation_policy_ids: list[str],
    user_ids: list[str],
    schedule_ids: list[str],
    since: str | None,
    until: str | None,
    earliest: bool,
    limit: int,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {"limit": str(min(limit, HARD_CAP))}
    for i, v in enumerate(escalation_policy_ids):
        params[f"escalation_policy_ids[{i}]"] = v
    for i, v in enumerate(user_ids):
        params[f"user_ids[{i}]"] = v
    for i, v in enumerate(schedule_ids):
        params[f"schedule_ids[{i}]"] = v
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if earliest:
        params["earliest"] = "true"
    payload = _get_json(
        client,
        connector,
        path=ONCALLS_PATH,
        verb="list on-calls",
        query_desc=json.dumps(params, sort_keys=True),
        query=params,
        candidates_path=None,
    )
    rows = payload.get("oncalls") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise click.ClickException(
            f"list on-calls: expected an object with an 'oncalls' array, got "
            f"{type(payload).__name__}"
        )
    return [row for row in rows if isinstance(row, dict)][:limit]


# --- incident search / get ---


def _build_incident_query(
    *,
    statuses: tuple[str, ...],
    urgencies: tuple[str, ...],
    service_ids: tuple[str, ...],
    incident_key: str | None,
    since: str | None,
    until: str | None,
    limit: int,
) -> dict[str, str]:
    params: dict[str, str] = {"limit": str(min(limit, HARD_CAP))}
    for i, s in enumerate(statuses):
        params[f"statuses[{i}]"] = s
    for i, u in enumerate(urgencies):
        params[f"urgencies[{i}]"] = u
    for i, sid in enumerate(service_ids):
        params[f"service_ids[{i}]"] = sid
    if incident_key:
        params["incident_key"] = incident_key
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    return params


def _search_incidents(
    client: mcp.MCPClient,
    connector: str,
    query: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    payload = _get_json(
        client,
        connector,
        path=INCIDENTS_PATH,
        verb="query incidents",
        query_desc=json.dumps(query, sort_keys=True),
        query=query,
        candidates_path=["incidents"],
    )
    rows = payload.get("incidents") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise click.ClickException(
            f"query incidents: expected an object with an 'incidents' array, "
            f"got {type(payload).__name__}"
        )
    return [row for row in rows if isinstance(row, dict)][:limit]


def _discover_incident_fields(client: mcp.MCPClient, connector: str) -> list[str]:
    rows = _search_incidents(
        client,
        connector,
        _build_incident_query(
            statuses=(),
            urgencies=(),
            service_ids=(),
            incident_key=None,
            since=None,
            until=None,
            limit=1,
        ),
        limit=1,
    )
    if not rows:
        return []
    return sorted(str(k) for k in rows[0].keys())


class _PagerDutySchemaProvider:
    """SchemaProvider for PagerDuty: incidents -> field.

    Only `incidents` is walkable. Services/escalation policies/priorities/
    on-calls are small, bounded configuration lookups the CLI always fetches
    fresh to pick a create-incident/update-incident target -- they are not
    incident-shaped operational data worth persisting in the knowledge graph,
    so they are excluded from RESOURCE_NAMES. Fields are sampled from one
    search row (PagerDuty has no dedicated schema-introspection endpoint), so
    refresh unions sampled field names rather than treating absence as
    authoritative deletion.
    """

    PACKAGE = PACKAGE
    PLATFORM = PLATFORM_LABEL
    TIERS_SINGULAR: tuple[str, ...] = ("Resource", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Resources", "Fields")
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
        if len(tiers) == 1 and tiers[0] == "incidents":
            return _discover_incident_fields(self._client, connector)
        return []


_EXAMPLES = """
Examples:

\b
  pagerduty.py list-services --query "SEC"
  pagerduty.py list-escalation-policies --query "On-Call"
  pagerduty.py list-priorities
  pagerduty.py list-oncalls --escalation-policy-id PT20YPA
  pagerduty.py list-incidents --status triggered --status acknowledged --limit 25
  pagerduty.py get --id PT4KHLK
  pagerduty.py view-result --ref <ref> --row 1
  pagerduty.py grep --pattern urgency
  pagerduty.py ls --path /

\b
  # create-incident fires immediately -- no --yes gate. See SKILL.md's
  # Guardrails for why, and always pass --incident-key.
  pagerduty.py create-incident --service PWIXJZS --title "Correlated critical: lateral movement + exfil" \\
      --urgency high --incident-key crogl-correlation-7f3a1c --details "..."

\b
  # update-incident / add-comment are gated -- run once WITHOUT --yes, show
  # the gate line to the user, then re-run with --yes.
  pagerduty.py update-incident --id PT4KHLK --status resolved
  pagerduty.py add-comment --id PT4KHLK --content "False positive: known scanner IP."
"""


@click.group(epilog=_EXAMPLES)
def cli() -> None:
    """Drive a PagerDuty connector. Run `<command> --help` for details."""


@cli.command(name="list-services")
@click.option("--query", default=None, help="Substring match against service name (server-side filter).")
@click.option("--name", default=None, help="Exact service name to match.")
@click.option("--team-id", "team_ids", multiple=True, help="Restrict to services owned by this team id; repeatable.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def list_services_cmd(query: str | None, name: str | None, team_ids: tuple[str, ...], limit: int) -> None:
    """List services (GET /services) -- resolve the --service id for create-incident."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _list_services(client, connector, query=query, name=name, team_ids=_list_ids(team_ids), limit=limit)
    _emit_rows(rows, command="list-services", connector=connector, query_desc=query or "", catalog_path=None)


@cli.command(name="list-escalation-policies")
@click.option("--query", default=None, help="Substring match against escalation policy name (server-side filter).")
@click.option("--user-id", "user_ids", multiple=True, help="Restrict to policies where this user is a target; repeatable.")
@click.option("--team-id", "team_ids", multiple=True, help="Restrict to policies owned by this team id; repeatable.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def list_escalation_policies_cmd(query: str | None, user_ids: tuple[str, ...], team_ids: tuple[str, ...], limit: int) -> None:
    """List escalation policies (GET /escalation_policies) -- resolve the --escalation-policy override id."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _list_escalation_policies(
            client, connector, query=query, user_ids=_list_ids(user_ids), team_ids=_list_ids(team_ids), limit=limit
        )
    _emit_rows(rows, command="list-escalation-policies", connector=connector, query_desc=query or "", catalog_path=None)


@cli.command(name="list-priorities")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def list_priorities_cmd(limit: int) -> None:
    """List priorities, most to least severe (GET /priorities) -- resolve the --priority id.

    Priorities is an opt-in PagerDuty account feature (Event Intelligence /
    Incident Priority). If it isn't enabled on this account, this returns an
    empty list -- not confirmed live as of this connector's initial version,
    see SKILL.md's changelog."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _list_priorities(client, connector, limit=limit)
    _emit_rows(rows, command="list-priorities", connector=connector, query_desc="", catalog_path=None)


@cli.command(name="list-oncalls")
@click.option("--escalation-policy-id", "escalation_policy_ids", multiple=True, help="Restrict to this escalation policy id; repeatable.")
@click.option("--user-id", "user_ids", multiple=True, help="Restrict to this user id; repeatable.")
@click.option("--schedule-id", "schedule_ids", multiple=True, help="Restrict to this schedule id; repeatable.")
@click.option("--since", default=None, help="ISO 8601 start of the time range (defaults to now).")
@click.option("--until", default=None, help="ISO 8601 end of the time range (defaults to now -- pass both --since/--until for a real window).")
@click.option("--earliest", is_flag=True, default=False, help="Only the earliest on-call per (escalation policy, level, user) -- useful for 'who is on call right now'.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def list_oncalls_cmd(
    escalation_policy_ids: tuple[str, ...],
    user_ids: tuple[str, ...],
    schedule_ids: tuple[str, ...],
    since: str | None,
    until: str | None,
    earliest: bool,
    limit: int,
) -> None:
    """List on-call entries (GET /oncalls) -- who is on call, for which escalation policy, right now or over a window."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _list_oncalls(
            client,
            connector,
            escalation_policy_ids=_list_ids(escalation_policy_ids),
            user_ids=_list_ids(user_ids),
            schedule_ids=_list_ids(schedule_ids),
            since=since,
            until=until,
            earliest=earliest,
            limit=limit,
        )
    _emit_rows(rows, command="list-oncalls", connector=connector, query_desc="", catalog_path=None)


@cli.command(name="list-incidents")
@click.option("--status", "statuses", type=click.Choice(INCIDENT_STATUS_VALUES), multiple=True, help="Incident status; repeatable (OR'd). Omit for all statuses.")
@click.option("--urgency", "urgencies", type=click.Choice(URGENCY_VALUES), multiple=True, help="Incident urgency; repeatable (OR'd).")
@click.option("--service-id", "service_ids", multiple=True, help="Restrict to this service id; repeatable.")
@click.option("--incident-key", default=None, help="Match a specific de-duplication key (see create-incident --incident-key).")
@click.option("--since", default=None, help="ISO 8601 start of the date range. Max range is 6 months; default is the last month.")
@click.option("--until", default=None, help="ISO 8601 end of the date range.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.option("--output", "output_path", type=click.Path(dir_okay=False, writable=True), default=None, help="Also write the result rows to FILE in the verifiable upload-samples format.")
def list_incidents_cmd(
    statuses: tuple[str, ...],
    urgencies: tuple[str, ...],
    service_ids: tuple[str, ...],
    incident_key: str | None,
    since: str | None,
    until: str | None,
    limit: int,
    output_path: str | None,
) -> None:
    """Search incidents (GET /incidents); emits grouped PSV with a ref for drill-down."""
    query = _build_incident_query(
        statuses=statuses,
        urgencies=urgencies,
        service_ids=service_ids,
        incident_key=incident_key,
        since=since,
        until=until,
        limit=limit,
    )
    query_desc = json.dumps(query, sort_keys=True)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_incidents(client, connector, query, limit)
    ref = _emit_rows(rows, command="list-incidents", connector=connector, query_desc=query_desc, catalog_path=["incidents"])
    if output_path and rows and ref is not None:
        samples.write_export(
            output_path,
            producer=f"{SKILL_NAME}/list-incidents",
            connector=connector,
            query=query_desc,
            ref=ref,
            rows=rows,
        )


@cli.command(name="query")
@click.option("--status", "statuses", type=click.Choice(INCIDENT_STATUS_VALUES), multiple=True, help="Incident status; repeatable (OR'd).")
@click.option("--urgency", "urgencies", type=click.Choice(URGENCY_VALUES), multiple=True, help="Incident urgency; repeatable (OR'd).")
@click.option("--service-id", "service_ids", multiple=True, help="Restrict to this service id; repeatable.")
@click.option("--incident-key", default=None, help="Match a specific de-duplication key.")
@click.option("--since", default=None, help="ISO 8601 start of the date range.")
@click.option("--until", default=None, help="ISO 8601 end of the date range.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
def query_cmd(
    statuses: tuple[str, ...],
    urgencies: tuple[str, ...],
    service_ids: tuple[str, ...],
    incident_key: str | None,
    since: str | None,
    until: str | None,
    limit: int,
) -> None:
    """Alias for list-incidents (PagerDuty's incident filters are flat REST params, not a query language)."""
    query = _build_incident_query(
        statuses=statuses,
        urgencies=urgencies,
        service_ids=service_ids,
        incident_key=incident_key,
        since=since,
        until=until,
        limit=limit,
    )
    query_desc = json.dumps(query, sort_keys=True)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_incidents(client, connector, query, limit)
    _emit_rows(rows, command="query", connector=connector, query_desc=query_desc, catalog_path=["incidents"])


@cli.command(name="get")
@click.option("--id", "incident_id", required=True, help="Incident id, e.g. PT4KHLK (see list-incidents).")
def get_cmd(incident_id: str) -> None:
    """Hydrate one incident's full record (GET /incidents/{id}) as JSON."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get_json(
            client,
            connector,
            path=INCIDENT_PATH.format(id=incident_id),
            verb="get incident",
            query_desc=_query_desc("get", incident_id=incident_id),
            query=None,
            candidates_path=["incidents"],
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="create-incident")
@click.option("--service", "service_id", required=True, help="Target service id (see list-services). Resolved by direct GET before creating -- an id that doesn't exist fails clearly rather than routing to the wrong service.")
@click.option("--title", required=True, help="Succinct incident title (nature/symptom/cause/effect).")
@click.option("--escalation-policy", "escalation_policy_id", default=None, help="Override the service's default escalation policy id (see list-escalation-policies). Cannot be combined with an existing escalation-in-progress on the same incident_key.")
@click.option("--priority", "priority_id", default=None, help="Priority id (see list-priorities). Not pre-validated -- an invalid id 400s with PagerDuty's own message.")
@click.option("--urgency", type=click.Choice(URGENCY_VALUES), default=None, help="Urgency override. Defaults to the service's own urgency rules if omitted.")
@click.option("--details", default=None, help="Longer incident body/details text.")
@click.option(
    "--incident-key",
    required=True,
    help=(
        "REQUIRED de-duplication key for this connector (PagerDuty itself "
        "treats it as optional). Reusing the same key for the same "
        "correlated detection makes a retried/duplicate create-incident call "
        "a no-op against PagerDuty's own de-dup instead of paging twice -- "
        "derive it deterministically from the detection (e.g. a hash of the "
        "correlated alert ids), not from a random value generated per call."
    ),
)
def create_incident_cmd(
    service_id: str,
    title: str,
    escalation_policy_id: str | None,
    priority_id: str | None,
    urgency: str | None,
    details: str | None,
    incident_key: str,
) -> None:
    """WRITE: create/escalate an incident (POST /incidents). Fires immediately -- NO --yes gate.

    This is the connector's core autonomous-escalation action: paging
    PagerDuty for a Crogl-correlated critical finding when there's no
    human SOC watching. See the module docstring's Write Posture and
    SKILL.md's Guardrails for why this write, uniquely, is not approval-gated."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        service_ref = _fetch_reference(client, connector, path=SERVICE_PATH, kind="service", value=service_id)
        incident: dict[str, Any] = {
            "type": "incident",
            "title": title,
            "service": {"id": service_ref["id"], "type": "service_reference"},
            "incident_key": incident_key,
        }
        if escalation_policy_id:
            policy_ref = _fetch_reference(
                client, connector, path=ESCALATION_POLICY_PATH, kind="escalation policy", value=escalation_policy_id
            )
            incident["escalation_policy"] = {"id": policy_ref["id"], "type": "escalation_policy_reference"}
        if priority_id:
            incident["priority"] = {"id": priority_id, "type": "priority_reference"}
        if urgency:
            incident["urgency"] = urgency
        if details:
            incident["body"] = {"type": "incident_body", "details": details}
        r = _write(
            client,
            connector,
            method="POST",
            path=INCIDENTS_PATH,
            payload={"incident": incident},
            verb="create incident",
            query_text=incident_key,
        )
    _emit_write_result(r, verb="create incident", done=f"Incident created against service {service_ref['id']} (incident_key={incident_key}).")


@cli.command(name="update-incident")
@click.option("--id", "incident_id", required=True, help="Incident id (see list-incidents).")
@click.option("--status", type=click.Choice(INCIDENT_STATUS_VALUES), default=None, help="New status. 'acknowledged' claims it; 'resolved' closes it; 'triggered' reopens a resolved incident.")
@click.option("--priority", "priority_id", default=None, help="Reprioritize to this priority id (see list-priorities).")
@click.option("--escalation-policy", "escalation_policy_id", default=None, help="Reassign to this escalation policy id. Cannot be combined with --assignee.")
@click.option("--assignee", "assignee_ids", multiple=True, help="Reassign to this user id; repeatable. Cannot be combined with --escalation-policy.")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def update_incident_cmd(
    incident_id: str,
    status: str | None,
    priority_id: str | None,
    escalation_policy_id: str | None,
    assignee_ids: tuple[str, ...],
    yes: bool,
) -> None:
    """WRITE: acknowledge/resolve/reopen, reprioritize, or reassign an EXISTING incident (PUT /incidents/{id}). Gated -- requires --yes."""
    if escalation_policy_id and assignee_ids:
        raise click.ClickException("pass --escalation-policy or --assignee, not both (PagerDuty rejects both on one update).")
    if not any([status, priority_id, escalation_policy_id, assignee_ids]):
        raise click.ClickException("nothing to update -- pass at least one of --status/--priority/--escalation-policy/--assignee.")

    incident: dict[str, Any] = {"type": "incident_reference"}
    intent_parts = [f"incident {incident_id}"]
    if status:
        incident["status"] = status
        intent_parts.append(f"status -> {status}")
    if priority_id:
        incident["priority"] = {"id": priority_id, "type": "priority_reference"}
        intent_parts.append(f"priority -> {priority_id}")
    if escalation_policy_id:
        incident["escalation_policy"] = {"id": escalation_policy_id, "type": "escalation_policy_reference"}
        intent_parts.append(f"escalation policy -> {escalation_policy_id}")
    if assignee_ids:
        incident["assignments"] = [
            {"assignee": {"id": uid, "type": "user_reference"}} for uid in assignee_ids
        ]
        intent_parts.append(f"assignees -> {', '.join(assignee_ids)}")

    _require_approval(yes, "update " + "; ".join(intent_parts))
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="PUT",
            path=INCIDENT_PATH.format(id=incident_id),
            payload={"incident": incident},
            verb="update incident",
            query_text=incident_id,
        )
    _emit_write_result(r, verb="update incident", done=f"Incident {incident_id} updated ({'; '.join(intent_parts[1:]) or 'no-op'}).")


@cli.command(name="add-comment")
@click.option("--id", "incident_id", required=True, help="Incident id (see list-incidents).")
@click.option("--content", required=True, help="Note text.")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def add_comment_cmd(incident_id: str, content: str, yes: bool) -> None:
    """WRITE: add a note to an incident (POST /incidents/{id}/notes). Gated -- requires --yes."""
    _require_approval(yes, f"add a note to incident {incident_id} as the connector's From user: {content!r}")
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="POST",
            path=INCIDENT_NOTES_PATH.format(id=incident_id),
            payload={"note": {"content": content}},
            verb="add comment",
            query_text=incident_id,
        )
    _emit_write_result(r, verb="add comment", done=f"Note added to incident {incident_id}.")


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior list-* or list-incidents/query call.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="1-based row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior list-*/list-incidents/query call."""
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
    """KG sample/lookup over incidents. Services/escalation policies/
    priorities/on-calls are not sample-able resources -- see
    _PagerDutySchemaProvider's docstring for why they're excluded from the
    KG catalog entirely."""
    if len(segments) != 1 or segments[0] != "incidents":
        return []
    if column is not None and column != "id":
        # No generic field=value search beyond the documented named params
        # above; unsupported columns yield no sample rather than silently
        # ignoring the requested column.
        return []
    if column == "id" and not values:
        return []
    query = _build_incident_query(
        statuses=(),
        urgencies=(),
        service_ids=(),
        incident_key=(values[0] if column == "id" and len(values) == 1 else None),
        since=None,
        until=None,
        limit=limit,
    )
    return _search_incidents(client, connector, query, limit)


samplecmds.add_commands(cli, _sample_rows)

kgcommands.add_commands(
    cli,
    lambda client: _PagerDutySchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label=PLATFORM_LABEL,
    ls_path_help="UNIX-style absolute schema path. ``/`` lists PagerDuty connectors this skill owns; ``/<connector>/incidents/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's PagerDuty connectors' schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: incidents -> Field. Services/escalation policies/priorities/\non-calls are not walkable -- see SKILL.md's Scope. Fields are sampled from\none search row, not discovered from a dedicated schema endpoint.",
    refresh_docstring="Walk the PagerDuty incidents catalog in real time and commit catalog tiers to the KG.\n\nThe only ``pagerduty`` command that hits the connector backend for schema\ndiscovery; ``ls`` reads exclusively from the KG cache. Walks are resumable\nper-tier. Because fields are sampled from one row, refresh unions field\nnames rather than treating absence as authoritative deletion.",
    grep_docstring="String-search this skill's PagerDuty connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact list-incidents/query parameter description the samples came from -- must match the source query call.",
    upload_ref_help="UUID of a recent list-incidents/query call (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `pagerduty.py list-incidents ... --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /incidents/<field>).",
    upload_docstring="Verify-then-upload samples from a recent list-incidents/query call.\n\nSource is either `--ref UUID` (the cache from a recent query call) or\n`--from-file FILE`. Exactly one is required. Both enforce a read-before-write\ncheck: the agent must have queried these exact rows with this exact query\ndescription, the export cannot exceed the upload limit, and a content hash\nmust match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
