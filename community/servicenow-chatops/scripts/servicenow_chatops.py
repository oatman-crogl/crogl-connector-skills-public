#!/usr/bin/env python3
"""servicenow_chatops CLI -- drives a ServiceNow "ChatOps verification" custom
connector via the MCP api_proxy tool.

This is NOT a general ServiceNow connector. It creates and reads records in
ONE table, configured per connector instance (the `target_table` credential
field, baked into the connector's base URL) -- the table the customer's own
ServiceNow ChatOps/Now Actions notification flow is already configured to
watch and deliver to Slack/Microsoft Teams. This connector does not talk to
Slack/Teams, and does not configure that delivery flow; it only writes and
reads records in the table that flow watches. If a broader, any-table
ServiceNow query connector is what's needed instead, that is a separate,
already-existing capability (the `crogl-servicenow` built-in skill) -- this
connector is deliberately narrower and adds the one thing that built-in does
not have: creating a new record (a write action requiring `--yes` approval).

Subcommands (read):
  check-reply     Fetch one verification-request record by sys_id -- the
                   record's current assignee, state, description, and the
                   `comments` journal field (every comment ever added, not
                   just the target user's latest reply -- see SKILL.md).
  query           List/search verification-request records, with an optional
                   raw ServiceNow encoded query (sysparm_query). Grouped PSV
                   with a ref for drill-down.
  view-result     Re-print one cached row (full record JSON) from a prior query.

Subcommands (write -- gated behind --yes):
  send-verification  WRITE: create a new record in the target table, assigned
                      to a target user, with a verification question. Nothing
                      is sent to ServiceNow until the exact mutation has been
                      shown to and approved by the user.
  add-comment         WRITE: add a comment to a record without changing state.
  close               WRITE: set the record's state (and optionally add a
                      closing comment) once the verification is resolved.

Auth is OAuth2 Client Credentials, injected server-side as an Authorization:
Bearer header from the connector's stored client_id/client_secret. The
target table is fixed at connector-configuration time (baked into the base
URL) -- deliberately not a per-call argument, so the agent cannot write into
a table the customer's ChatOps flow isn't actually watching.
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
    mcp,
    proxy,
    queryerror,
)
from _lib.psv import Options, marshal_grouped

SKILL_NAME = "servicenow-chatops"
PACKAGE = "servicenow-chatops"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "servicenow_chatops.py view-result"

DEFAULT_LIMIT = 10
HARD_CAP = 25

# The fixed field this connector writes the target user to and reads the
# assignee from. Hardcoded rather than configurable: there is no verified
# runtime mechanism for a community connector script to read a non-secret
# connector.json field that isn't baked into the HTTP binding (base_url /
# headers) -- see the provisioning guide. `assigned_to` is standard on
# ServiceNow's Task-derived tables (incident, sc_task, interaction); a fully
# custom target_table must have a field of this name for send-verification /
# check-reply to work.
USER_FIELD = "assigned_to"

DEFAULT_RECORD_FIELDS = "sys_id,number,short_description,assigned_to,state,sys_created_on"


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _record_path(sys_id: str | None = None) -> str:
    """Path relative to the connector's base URL, which already ends in
    /api/now/table/<target_table> -- so the record root is "" and one record
    is "/<sys_id>"."""
    return f"/{sys_id}" if sys_id else ""


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


def _parse_result_list(body: str, what: str) -> list[dict[str, Any]]:
    payload = _parse_json(body, what)
    if not isinstance(payload, dict):
        raise click.ClickException(f"{what}: unexpected response: {body[:300]}")
    result = payload.get("result")
    if not isinstance(result, list):
        raise click.ClickException(
            f"{what}: response has no result array: {body[:300]}"
        )
    return [r for r in result if isinstance(r, dict)]


def _parse_result_object(body: str, what: str) -> dict[str, Any]:
    payload = _parse_json(body, what)
    if not isinstance(payload, dict):
        raise click.ClickException(f"{what}: unexpected response: {body[:300]}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise click.ClickException(
            f"{what}: response has no result object: {body[:300]}"
        )
    return result


def _field_display(field: Any) -> Any:
    """The human-readable form of a sysparm_display_value=all field
    ({"display_value", "value"}); an unwrapped field is returned as-is."""
    if isinstance(field, dict) and "display_value" in field:
        return field["display_value"]
    return field


def _get(
    client: mcp.MCPClient,
    connector: str,
    path: str,
    *,
    query: dict[str, str],
    verb: str,
    query_text: str | None,
) -> Any:
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=path,
        query=query,
        headers={"Accept": "application/json"},
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="snquery",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_text,
            response=r,
            candidates_path=None,
            client=client,
        )
    return r.body


def _require_approval(yes: bool, intent: str) -> None:
    """Approval gate for every write verb. Raised BEFORE any MCP/ServiceNow
    call, so a gated command has no side effect at all -- it only reports the
    mutation it would perform. The agent must relay that line to the user,
    get an explicit yes, and only then re-run with --yes. Never pass --yes on
    the first attempt, and never treat a general instruction ("check in with
    everyone flagged this week") as approval for one specific mutation."""
    if not yes:
        raise click.ClickException(
            f"WRITE gated: would {intent}. Nothing was sent to ServiceNow. Show "
            "this to the user and re-run with --yes only after they explicitly "
            "approve this exact action."
        )


def _write(
    client: mcp.MCPClient,
    connector: str,
    *,
    method: str,
    path: str,
    payload: dict[str, Any],
    verb: str,
    query_text: str | None,
) -> proxy.ProxyResponse:
    """Send one mutating request. Never retried: a re-sent POST/PATCH would
    double the side effect upstream, and for send-verification specifically
    would notify the target user twice."""
    r = proxy.dispatch(
        client,
        connector,
        method=method,
        path=path,
        query={"sysparm_input_display_value": "true"},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        body=json.dumps(payload),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="snquery",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_text,
            response=r,
            candidates_path=None,
            client=client,
        )
    return r


def _parse_field_overrides(pairs: tuple[str, ...]) -> dict[str, Any]:
    """Parse repeated --field KEY=VALUE into a fields dict, for any field a
    customer's target_table requires beyond assigned_to/short_description/
    description (instance-specific -- e.g. a mandatory category or a custom
    field). VALUE is decoded as JSON when it parses, kept as a plain string
    otherwise."""
    fields: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise click.ClickException(
                f"--field expects KEY=VALUE, got {pair!r} (e.g. --field category=security)"
            )
        try:
            fields[key] = json.loads(raw)
        except ValueError:
            fields[key] = raw
    return fields


def _emit_rows(
    rows: list[dict[str, Any]], *, command: str, connector: str, query: str
) -> None:
    """Marshal rows to grouped PSV, cache under a fresh ref for drill-down,
    and print. An empty result prints an explicit 0-row line (never a blank
    line) so the agent doesn't mistake success-with-no-data for a failed call
    and retry, matching this repo's qradar/_emit_rows convention."""
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/{command}"

    if not rows:
        query_note = f" for {query!r}" if query else ""
        click.echo(f"No {command} results (0 rows){query_note}.")
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
        catalog_path=None,
    )
    click.echo(body_text)


_EXAMPLES = """
Examples:

\b
  # write -- run once WITHOUT --yes, show the gate line to the user, then re-run
  servicenow_chatops.py send-verification --user john.smith \\
      --question "Are you traveling to Budapest this week, and was it authorized through HR?" \\
      --short-description "Credential compromise verification"
  servicenow_chatops.py send-verification --user john.smith --question "..." --yes

\b
  servicenow_chatops.py check-reply --sys-id <sys_id>
  servicenow_chatops.py query --limit 10
  servicenow_chatops.py query --query "active=true^assigned_to.email=john.smith@example.com"
  servicenow_chatops.py view-result --ref <ref> --row 1
  servicenow_chatops.py add-comment --sys-id <sys_id> --message "Following up shortly." --yes
  servicenow_chatops.py close --sys-id <sys_id> --state 7 --note "Verified: authorized travel." --yes
"""


@click.group(epilog=_EXAMPLES)
def cli() -> None:
    """Drive a ServiceNow ChatOps-verification connector. Run `<command> --help` for details."""


@cli.command(name="send-verification")
@click.option(
    "--user",
    "target_user",
    required=True,
    help="Target user's ServiceNow username or email (set on the assigned_to "
    "field via sysparm_input_display_value; resolved to a sys_id by ServiceNow).",
)
@click.option(
    "--question",
    required=True,
    help="The verification question / context for the target user, written into "
    "the record's description field.",
)
@click.option(
    "--short-description",
    default="Crogl ChatOps verification request",
    show_default=True,
    help="One-line summary field.",
)
@click.option(
    "--field",
    "field_overrides",
    multiple=True,
    help="Extra field as KEY=VALUE; repeatable. Use for any field this instance's "
    "target table requires beyond assigned_to/short_description/description "
    "(e.g. --field category=security). Explicit --field wins over the "
    "convenience options.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required to actually write. Without it, the command prints the intended "
    "action and exits.",
)
def send_verification(
    target_user: str,
    question: str,
    short_description: str,
    field_overrides: tuple[str, ...],
    yes: bool,
) -> None:
    """WRITE: create a verification-request record assigned to a target user.
    Gated -- requires --yes.

    POST to the connector's configured target table. Whether this actually
    reaches the target user in chat depends entirely on the customer's own
    ServiceNow ChatOps/Now Actions flow already watching this table for
    assignment -- this command only creates the record; it does not talk to
    Slack/Teams itself. A created record cannot be deleted from this
    connector, so a mistaken send leaves a real record (and, if the
    customer's ChatOps flow is live, a real chat notification to a real
    person) behind."""
    fields: dict[str, Any] = {
        USER_FIELD: target_user,
        "short_description": short_description,
        "description": question,
    }
    fields.update(_parse_field_overrides(field_overrides))

    _require_approval(
        yes,
        f"create a record in the connector's configured target table, assigned to "
        f"{target_user!r}, titled {short_description!r}, with description "
        f"{question!r} (fields: {json.dumps(fields, sort_keys=True)}). If the "
        f"customer's ServiceNow ChatOps flow is watching this table, {target_user} "
        f"will be notified in chat",
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="POST",
            path=_record_path(),
            payload=fields,
            verb="send verification",
            query_text=None,
        )
        result = _parse_result_object(r.body, "send verification")
    sys_id = _field_display(result.get("sys_id"))
    number = _field_display(result.get("number"))
    click.echo(
        f"Verification request created (sys_id={sys_id}"
        + (f", number={number}" if number else "")
        + f"), assigned to {target_user}. Use check-reply --sys-id {sys_id} to poll for a response."
    )


@cli.command(name="check-reply")
@click.option("--sys-id", "sys_id", required=True, help="The record's sys_id.")
def check_reply(sys_id: str) -> None:
    """Fetch one verification-request record by sys_id (read-only).

    GET on the connector's configured target table. The `comments` field is
    ServiceNow's journal field -- it returns the FULL concatenated comment
    history (every comment ever added by anyone, each individually
    timestamped/attributed), not just the target user's latest reply. This
    connector has not been live-tested, so the exact string format of that
    journal blob is not confirmed -- treat it as free text to read, not a
    structured field to parse."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        body = _get(
            client,
            connector,
            _record_path(sys_id),
            query={
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            verb="check reply",
            query_text=sys_id,
        )
        record = _parse_result_object(body, "check reply")
    flattened = {name: _field_display(field) for name, field in record.items()}
    click.echo(json.dumps(flattened, indent=2))


@cli.command(name="query")
@click.option(
    "--query",
    "sysparm_query",
    default="",
    show_default=False,
    help='ServiceNow encoded query (sysparm_query), e.g. "active=true^assigned_to.email=john.smith@example.com". '
    "`^` is AND, `^OR` is OR. Omit to list recent rows. Field names and state/priority "
    "values are instance-customizable -- confirm with the customer's ServiceNow admin "
    "before assuming a value.",
)
@click.option(
    "--fields",
    default=DEFAULT_RECORD_FIELDS,
    show_default=False,
    help="Comma-separated fields to return (sysparm_fields). Default: the rendered columns.",
)
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows (hard cap {HARD_CAP}).",
)
def query_records(sysparm_query: str, fields: str, limit: int) -> None:
    """List/search verification-request records on the connector's configured
    target table via an optional encoded query; emits grouped PSV with a ref
    for drill-down."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        params = {
            "sysparm_limit": str(limit),
            "sysparm_display_value": "true",
            "sysparm_exclude_reference_link": "true",
            "sysparm_fields": fields,
        }
        if sysparm_query:
            params["sysparm_query"] = sysparm_query
        body = _get(
            client,
            connector,
            _record_path(),
            query=params,
            verb="query",
            query_text=sysparm_query,
        )
        rows = _parse_result_list(body, "query")[:limit]
    _emit_rows(rows, command="query", connector=connector, query=sysparm_query)


@cli.command(name="add-comment")
@click.option("--sys-id", "sys_id", required=True, help="The record's sys_id.")
@click.option("--message", required=True, help="Comment text, added to the comments journal field.")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required to actually write. Without it, the command prints the intended "
    "action and exits.",
)
def add_comment(sys_id: str, message: str, yes: bool) -> None:
    """WRITE: add a comment to a record without changing its state. Gated --
    requires --yes.

    PATCH the comments journal field. If the customer's ChatOps flow also
    notifies on comment updates (not just assignment), this may itself
    generate a chat notification -- treat it with the same care as
    send-verification, not as a free-form note."""
    _require_approval(yes, f"add a comment to {sys_id}: {message!r}")
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="PATCH",
            path=_record_path(sys_id),
            payload={"comments": message},
            verb="add comment",
            query_text=sys_id,
        )
        _parse_result_object(r.body, "add comment")
    click.echo(f"Comment added to {sys_id}.")


@cli.command(name="close")
@click.option("--sys-id", "sys_id", required=True, help="The record's sys_id.")
@click.option(
    "--state",
    required=True,
    help="Target state value for this table (raw, instance-defined -- e.g. "
    "ServiceNow's stock incident table often uses 7 for Closed, but this is "
    "NOT guaranteed on a customized table; confirm with the customer's admin "
    "or a prior `list`/`check-reply` before choosing one).",
)
@click.option(
    "--note",
    default="",
    show_default=False,
    help="Optional closing comment, added to the comments journal field in the same write.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required to actually write. Without it, the command prints the intended "
    "action and exits.",
)
def close_record(sys_id: str, state: str, note: str, yes: bool) -> None:
    """WRITE: set a verification-request record's state, optionally with a
    closing comment. Gated -- requires --yes.

    PATCH on the connector's configured target table. State values are
    instance-defined on ServiceNow tables (same caveat as Jira's status
    field) -- verify the value with the customer's admin or a `list` call
    before guessing."""
    payload: dict[str, Any] = {"state": state}
    if note:
        payload["comments"] = note
    _require_approval(
        yes,
        f"set state={state!r} on {sys_id}"
        + (f" with the closing comment {note!r}" if note else ""),
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="PATCH",
            path=_record_path(sys_id),
            payload=payload,
            verb="close",
            query_text=sys_id,
        )
        _parse_result_object(r.body, "close")
    click.echo(f"{sys_id} updated to state={state}.")


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior list.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="Row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query (pure cache read)."""
    try:
        record = cache.get_row(ref, result_set, row - 1)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
