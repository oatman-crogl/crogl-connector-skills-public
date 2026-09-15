#!/usr/bin/env python3
"""jira CLI -- drives a Jira Data Center custom connector via the MCP api_proxy tool.

Jira Data Center (on-prem), NOT Jira Cloud. The REST surface is v2
(``/rest/api/2/...``); the query language is JQL.

Subcommands (read):
  search-issues    Search issues via a JQL expression (GET /rest/api/2/search).
                   Emits grouped PSV with a ref for drill-down.
  query            Alias for search-issues (JQL is Jira's query language).
  get-issue        Hydrate one issue by key (e.g. PROJ-123) -- full detail card.
  list-projects    List projects (GET /rest/api/2/project).
  list-fields      List all fields, System + Custom (GET /rest/api/2/field) --
                   use this to resolve instance-specific field identifiers.
  list-issue-types List issue types (GET /rest/api/2/issuetype).
  list-transitions List the transitions available on one issue right now --
                   resolve a target status to a transition id before writing.
  create-meta      Required/allowed fields for create-issue on a project +
                   issue type (GET /rest/api/2/issue/createmeta).
  view-result      Re-print one row (full entity JSON) from a prior search.

Subcommands (write -- every one gated behind --yes):
  add-comment      WRITE: add a comment to an issue.
  create-issue     WRITE: create an issue in a project.
  transition-issue WRITE: move an issue through a workflow transition.
  assign-issue     WRITE: set or clear an issue's assignee.

Writes are gated: without --yes the command prints the exact mutation it would
perform and exits non-zero without contacting Jira. Only pass --yes after the
user has approved that specific action. There are no bulk, delete, or
arbitrary-field-update verbs by design.

Auth is a Personal Access Token (Jira DC 8.14+), injected server-side as the
``Authorization: Bearer`` header from the connector's stored secret. TLS
verification posture is controlled by the connector's ``skip_tls_verification``
flag (typically enabled for on-prem installs with self-signed certs). This
skill is bound to one configured connector and resolves it automatically; you
don't pass a connector name.

The PAT's Jira user must hold the matching project permission for each write
(Add comments, Create issues, Transition issues, Assign issues); a token
provisioned read-only returns 403 on the write verbs and nothing else changes.
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

SKILL_NAME = "jira-datacenter"
PACKAGE = "jira-datacenter"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "jira.py view-result"

# Context-budget rules (mirror the SKILL.md). A list call defaults to 10 rows;
# even an explicit "all"/"everything" request may not exceed the hard cap.
DEFAULT_LIMIT = 10
HARD_CAP = 25

# Trim the default issue payload to the columns the renderer shows -- a bare
# search returns every field (including large custom-field and rendered-body
# blobs). Callers can widen with --fields.
DEFAULT_ISSUE_FIELDS = (
    "summary,status,priority,assignee,reporter,created,updated,issuetype,project"
)


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _headers() -> dict[str, str]:
    """Per-call headers. The connector injects Authorization server-side; the
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


def _get(
    client: mcp.MCPClient,
    connector: str,
    path: str,
    *,
    query: dict[str, str] | None,
    verb: str,
    interface: str,
    query_text: str | None,
) -> Any:
    """GET a Jira REST v2 endpoint and return the decoded JSON payload.

    On a >=400 response, raise the shared actionable QueryError so JQL/field
    mistakes come back with a grammar hint and a worked example rather than a
    raw stack trace."""
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
            interface=interface,
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_text,
            response=r,
            candidates_path=None,
            client=client,
        )
    return _parse_json(r.body, verb)


def _require_approval(yes: bool, intent: str) -> None:
    """Approval gate for every write verb.

    Raised BEFORE any MCP/Jira call, so a gated command has no side effect at
    all -- it only reports the mutation it would perform. The agent must relay
    that line to the user, get an explicit yes, and only then re-run with
    ``--yes``. Never pass ``--yes`` on the first attempt, and never treat a
    general "go fix my Jira" as approval for a specific mutation."""
    if not yes:
        raise click.ClickException(
            f"WRITE gated: would {intent}. Nothing was sent to Jira. Show this "
            "to the user and re-run with --yes only after they explicitly "
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

    Never retried: a re-sent POST would double the side effect upstream (this is
    also why ``proxy.dispatch_retrying`` refuses non-idempotent methods). On a
    >=400 the shared actionable QueryError carries the vendor's own
    ``errorMessages``/``errors`` map, which for Jira writes is usually the
    specific field or permission at fault."""
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
            interface="rest-key",
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
    """Print a write's outcome. Several Jira write endpoints answer 204 with an
    empty body (transitions, assignee), so an empty body is success, not a
    parse failure -- print an explicit confirmation line instead."""
    body = r.body.strip()
    if not body:
        click.echo(done)
        return
    try:
        click.echo(json.dumps(json.loads(body), indent=2))
    except ValueError:
        click.echo(f"{verb}: {r.status_code} {body[:500]}")


def _parse_field_overrides(pairs: tuple[str, ...]) -> dict[str, Any]:
    """Parse repeated ``--field KEY=VALUE`` into a fields dict.

    VALUE is decoded as JSON when it parses (so ``customfield_10001={"id":"2"}``
    and ``--field labels=["a","b"]`` work) and kept as a plain string otherwise.
    Field identifiers are instance-specific: resolve them with `list-fields` or
    `create-meta`, never from vendor docs or memory."""
    fields: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise click.ClickException(
                f"--field expects KEY=VALUE, got {pair!r} (e.g. "
                '--field customfield_10010=Team-A)'
            )
        try:
            fields[key] = json.loads(raw)
        except ValueError:
            fields[key] = raw
    return fields


def _list_transitions(
    client: mcp.MCPClient, connector: str, issue_key: str
) -> list[dict[str, Any]]:
    """GET the transitions currently available on an issue.

    Transition ids are workflow-specific, not global, and the available set
    depends on the issue's current status and the caller's permissions -- so
    this must be read per issue, at the time of the write."""
    payload = _get(
        client,
        connector,
        f"/rest/api/2/issue/{issue_key}/transitions",
        query={"expand": "transitions.fields"},
        verb="list transitions",
        interface="rest-key",
        query_text=issue_key,
    )
    transitions = payload.get("transitions") if isinstance(payload, dict) else None
    if not isinstance(transitions, list):
        raise click.ClickException(
            f"list transitions: expected an object with a 'transitions' array, "
            f"got {type(payload).__name__}"
        )
    return [t for t in transitions if isinstance(t, dict)]


def _resolve_transition(
    transitions: list[dict[str, Any]], wanted: str
) -> dict[str, Any]:
    """Match ``wanted`` against a transition id, transition name, or target
    status name (case-insensitive). Ambiguity and misses both raise with the
    real available set, so the agent never guesses a second time."""
    needle = wanted.strip().casefold()
    matches = [
        t
        for t in transitions
        if str(t.get("id", "")).casefold() == needle
        or str(t.get("name", "")).casefold() == needle
        or str((t.get("to") or {}).get("name", "")).casefold() == needle
    ]
    available = ", ".join(
        f"{t.get('id')}={t.get('name')} (-> {(t.get('to') or {}).get('name')})"
        for t in transitions
    ) or "none"
    if not matches:
        raise click.ClickException(
            f"no transition matching {wanted!r} is available on this issue right "
            f"now. Available: {available}"
        )
    if len({str(t.get("id")) for t in matches}) > 1:
        raise click.ClickException(
            f"{wanted!r} matches more than one transition; pass the id. "
            f"Available: {available}"
        )
    return matches[0]


def _search_issues(
    client: mcp.MCPClient, connector: str, jql: str, fields: str, limit: int
) -> list[dict[str, Any]]:
    """GET /rest/api/2/search. The response is an object
    ``{startAt, maxResults, total, issues: [...]}`` -- unwrap ``issues``.
    ``limit`` is bounded by HARD_CAP, so a single page always suffices."""
    query: dict[str, str] = {
        "jql": jql,
        "startAt": "0",
        "maxResults": str(min(limit, HARD_CAP)),
    }
    if fields:
        query["fields"] = fields
    payload = _get(
        client,
        connector,
        "/rest/api/2/search",
        query=query,
        verb="search issues",
        interface="jql",
        query_text=jql,
    )
    issues = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(issues, list):
        raise click.ClickException(
            f"search issues: expected an object with an 'issues' array, got "
            f"{type(payload).__name__}"
        )
    return [row for row in issues if isinstance(row, dict)][:limit]


def _emit_rows(
    ctx: click.Context,
    rows: list[dict[str, Any]],
    *,
    command: str,
    connector: str,
    query: str,
    catalog_path: list[str] | None,
) -> None:
    """Marshal rows to grouped PSV, cache them under a fresh ref for drill-down,
    and print. An empty result prints an explicit 0-row line (never a blank
    line) so the agent doesn't mistake success-with-no-data for a failed call
    and retry."""
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
        catalog_path=catalog_path,
    )
    click.echo(body_text)


_EXAMPLES = """
Examples:

\b
  jira.py search-issues --jql "project = SEC AND status = Open" --limit 10
  jira.py search-issues --jql "priority = Blocker AND created >= -24h" --limit 5
  jira.py get-issue --key SEC-4141
  jira.py list-projects --limit 25
  jira.py list-fields
  jira.py view-result --ref <ref> --row 1

\b
  # writes -- run once WITHOUT --yes, show the gate line to the user, then re-run
  jira.py add-comment --key SEC-4141 --body "Triaged: matches known-good baseline."
  jira.py create-issue --project SEC --issuetype Bug --summary "Beaconing from 10.0.0.5"
  jira.py list-transitions --key SEC-4141
  jira.py transition-issue --key SEC-4141 --transition "In Progress"
  jira.py assign-issue --key SEC-4141 --assignee jdoe
"""


@click.group(epilog=_EXAMPLES)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Drive a Jira Data Center connector. Run `<command> --help` for details."""
    ctx.obj = {}


def _search_command(
    ctx: click.Context, jql: str, fields: str, limit: int, *, label: str
) -> None:
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _search_issues(client, connector, jql, fields, limit)
    _emit_rows(
        ctx,
        rows,
        command=label,
        connector=connector,
        query=jql,
        catalog_path=None,
    )


@cli.command(name="search-issues")
@click.option(
    "--jql",
    required=True,
    help='JQL expression, e.g. "project = SEC AND status = Open ORDER BY created DESC". '
    "Quote string values; `~` is contains; `IS EMPTY` for null.",
)
@click.option(
    "--fields",
    default=DEFAULT_ISSUE_FIELDS,
    show_default=False,
    help="Comma-separated fields to return (default: the rendered columns). "
    "Use `*all` for every field.",
)
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows (hard cap {HARD_CAP}).",
)
@click.pass_context
def search_issues(ctx: click.Context, jql: str, fields: str, limit: int) -> None:
    """Search issues via JQL; emits grouped PSV with a ref for drill-down."""
    _search_command(ctx, jql, fields, limit, label="search-issues")


@cli.command(name="query")
@click.option("--jql", required=True, help="JQL expression; Jira's query language.")
@click.option("--fields", default=DEFAULT_ISSUE_FIELDS, show_default=False, help="Comma-separated fields to return.")
@click.option("--limit", default=DEFAULT_LIMIT, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.pass_context
def query(ctx: click.Context, jql: str, fields: str, limit: int) -> None:
    """Alias for `search-issues` -- Jira's query language is JQL."""
    _search_command(ctx, jql, fields, limit, label="query")


@cli.command(name="get-issue")
@click.option("--key", "issue_key", required=True, help="Issue key, e.g. SEC-4141 (not a JQL query).")
@click.option("--fields", default="", show_default=False, help="Optional comma-separated fields to return (default: all).")
def get_issue(issue_key: str, fields: str) -> None:
    """Hydrate one issue by key and print the full JSON detail card."""
    query_params = {"fields": fields} if fields else {}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        issue = _get(
            client,
            connector,
            f"/rest/api/2/issue/{issue_key}",
            query=query_params,
            verb="get issue",
            interface="rest-key",
            query_text=issue_key,
        )
    click.echo(json.dumps(issue, indent=2))


@cli.command(name="list-projects")
@click.option("--limit", default=HARD_CAP, show_default=True, type=click.IntRange(1, HARD_CAP), help=f"Max rows (hard cap {HARD_CAP}).")
@click.pass_context
def list_projects(ctx: click.Context, limit: int) -> None:
    """List projects; emits grouped PSV with a ref for drill-down.

    GET /rest/api/2/project returns a bare JSON array (not paginated on Jira
    Data Center), so the limit is applied client-side."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get(
            client,
            connector,
            "/rest/api/2/project",
            query={},
            verb="list projects",
            interface="rest-key",
            query_text=None,
        )
        if not isinstance(payload, list):
            raise click.ClickException(
                f"list projects: expected a JSON array, got {type(payload).__name__}"
            )
        rows = [row for row in payload if isinstance(row, dict)][:limit]
    _emit_rows(ctx, rows, command="list-projects", connector=connector, query="", catalog_path=None)


@cli.command(name="list-fields")
def list_fields() -> None:
    """List all fields (System + Custom) to resolve instance-specific field IDs.

    GET /rest/api/2/field returns a bare JSON array; printed as-is so the agent
    can map a human field name to the identifier JQL expects on this instance."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get(
            client,
            connector,
            "/rest/api/2/field",
            query={},
            verb="list fields",
            interface="rest-key",
            query_text=None,
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="list-issue-types")
def list_issue_types() -> None:
    """List issue types (id + name) -- resolve the `--issuetype` for create-issue.

    GET /rest/api/2/issuetype returns a bare JSON array of every type on the
    instance; a given project may accept only a subset (see `create-meta`)."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get(
            client,
            connector,
            "/rest/api/2/issuetype",
            query={},
            verb="list issue types",
            interface="rest-key",
            query_text=None,
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="list-transitions")
@click.option("--key", "issue_key", required=True, help="Issue key, e.g. SEC-4141.")
def list_transitions(issue_key: str) -> None:
    """List the transitions available on one issue right now (read-only).

    Run this before `transition-issue`: ids are workflow-specific and the
    available set depends on the issue's current status."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        transitions = _list_transitions(client, connector, issue_key)
    click.echo(json.dumps(transitions, indent=2))


@cli.command(name="create-meta")
@click.option("--project", required=True, help="Project key, e.g. SEC.")
@click.option("--issuetype", default="", show_default=False, help="Optional issue-type name to narrow the response (e.g. Bug).")
def create_meta(project: str, issuetype: str) -> None:
    """Show the fields create-issue accepts on a project + issue type (read-only).

    GET /rest/api/2/issue/createmeta with the field expansion. Use it to find
    which fields are `required` on this instance -- a required custom field is
    the usual cause of a create-issue 400. Instances with many projects return a
    large payload, so always pass `--project`."""
    query = {"projectKeys": project, "expand": "projects.issuetypes.fields"}
    if issuetype:
        query["issuetypeNames"] = issuetype
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        payload = _get(
            client,
            connector,
            "/rest/api/2/issue/createmeta",
            query=query,
            verb="create meta",
            interface="rest-key",
            query_text=project,
        )
    click.echo(json.dumps(payload, indent=2))


@cli.command(name="add-comment")
@click.option("--key", "issue_key", required=True, help="Issue key, e.g. SEC-4141.")
@click.option("--body", "comment_body", required=True, help="Comment text (Jira wiki markup, not ADF -- this is Data Center).")
@click.option("--visibility-role", "visibility_role", default="", show_default=False, help="Optional project role name to restrict the comment to (e.g. Administrators). Omit for a comment visible to anyone who can see the issue.")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def add_comment(issue_key: str, comment_body: str, visibility_role: str, yes: bool) -> None:
    """WRITE: add a comment to an issue. Gated -- requires --yes.

    POST /rest/api/2/issue/{key}/comment. The comment is attributed to the PAT's
    Jira user, is visible to everyone who can see the issue unless
    `--visibility-role` narrows it, and cannot be retracted from here."""
    audience = (
        f"visible only to the {visibility_role!r} project role"
        if visibility_role
        else "visible to everyone who can see the issue"
    )
    _require_approval(
        yes,
        f"add a comment to {issue_key} as the connector's Jira user, {audience}: "
        f"{comment_body!r}",
    )
    payload: dict[str, Any] = {"body": comment_body}
    if visibility_role:
        payload["visibility"] = {"type": "role", "value": visibility_role}
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="POST",
            path=f"/rest/api/2/issue/{issue_key}/comment",
            payload=payload,
            verb="add comment",
            query_text=issue_key,
        )
    _emit_write_result(r, verb="add comment", done=f"Comment added to {issue_key}.")


@cli.command(name="create-issue")
@click.option("--project", required=True, help="Project key, e.g. SEC (see list-projects).")
@click.option("--summary", required=True, help="One-line summary.")
@click.option("--issuetype", required=True, help="Issue-type name, e.g. Bug or Task (see list-issue-types / create-meta).")
@click.option("--description", default="", show_default=False, help="Issue description (Jira wiki markup).")
@click.option("--priority", default="", show_default=False, help="Priority name, e.g. Major. Instance-defined -- confirm with create-meta.")
@click.option("--assignee", default="", show_default=False, help="Assignee username (Data Center uses the username, not an account id).")
@click.option("--label", "labels", multiple=True, help="Label to attach; repeatable. Jira rejects labels containing spaces.")
@click.option("--parent", default="", show_default=False, help="Parent issue key -- required when --issuetype is a sub-task type.")
@click.option("--field", "field_overrides", multiple=True, help='Extra field as KEY=VALUE; repeatable. VALUE is parsed as JSON when it parses, so quote a string that looks numeric (--field code=\'"12345"\'). Resolve ids with create-meta.')
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def create_issue(
    project: str,
    summary: str,
    issuetype: str,
    description: str,
    priority: str,
    assignee: str,
    labels: tuple[str, ...],
    parent: str,
    field_overrides: tuple[str, ...],
    yes: bool,
) -> None:
    """WRITE: create an issue in a project. Gated -- requires --yes.

    POST /rest/api/2/issue. A created issue can't be deleted from this
    connector, so a mistaken create leaves a real ticket behind -- prefer
    `create-meta` first when unsure which fields the project requires."""
    fields: dict[str, Any] = {
        "project": {"key": project},
        "summary": summary,
        "issuetype": {"name": issuetype},
    }
    if description:
        fields["description"] = description
    if priority:
        fields["priority"] = {"name": priority}
    if assignee:
        fields["assignee"] = {"name": assignee}
    if labels:
        fields["labels"] = list(labels)
    if parent:
        fields["parent"] = {"key": parent}
    # Explicit --field wins over the convenience options above, so an instance
    # with a customized schema can always be satisfied without a code change.
    fields.update(_parse_field_overrides(field_overrides))

    _require_approval(
        yes,
        f"create a new {issuetype} in project {project} titled {summary!r} "
        f"(fields: {json.dumps(fields, sort_keys=True)})",
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="POST",
            path="/rest/api/2/issue",
            payload={"fields": fields},
            verb="create issue",
            query_text=project,
        )
    _emit_write_result(r, verb="create issue", done=f"Issue created in {project}.")


@cli.command(name="transition-issue")
@click.option("--key", "issue_key", required=True, help="Issue key, e.g. SEC-4141.")
@click.option("--transition", "transition", required=True, help="Transition id, transition name, or target status name (see list-transitions).")
@click.option("--comment", default="", show_default=False, help="Optional comment to post as part of the transition.")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def transition_issue(issue_key: str, transition: str, comment: str, yes: bool) -> None:
    """WRITE: move an issue through a workflow transition. Gated -- requires --yes.

    POST /rest/api/2/issue/{key}/transitions. The requested transition is
    resolved against the issue's *currently available* transitions first, so a
    stale or wrong id fails with the real options rather than a bare 400. A
    workflow may fire post-functions (notifications, assignment, resolution) --
    the effect can be wider than a status change and may not be reversible."""
    _require_approval(
        yes,
        f"transition {issue_key} via {transition!r}"
        + (f" with the comment {comment!r}" if comment else ""),
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        target = _resolve_transition(
            _list_transitions(client, connector, issue_key), transition
        )
        payload: dict[str, Any] = {"transition": {"id": str(target.get("id"))}}
        if comment:
            payload["update"] = {"comment": [{"add": {"body": comment}}]}
        r = _write(
            client,
            connector,
            method="POST",
            path=f"/rest/api/2/issue/{issue_key}/transitions",
            payload=payload,
            verb="transition issue",
            query_text=issue_key,
        )
    to_status = (target.get("to") or {}).get("name", "?")
    _emit_write_result(
        r,
        verb="transition issue",
        done=f"{issue_key} transitioned via {target.get('name')} -> {to_status}.",
    )


@cli.command(name="assign-issue")
@click.option("--key", "issue_key", required=True, help="Issue key, e.g. SEC-4141.")
@click.option("--assignee", default="", show_default=False, help="Assignee username (Data Center username, not an email or account id).")
@click.option("--unassign", is_flag=True, default=False, help="Clear the assignee instead of setting one.")
@click.option("--yes", is_flag=True, default=False, help="Required to actually write. Without it, the command prints the intended action and exits.")
def assign_issue(issue_key: str, assignee: str, unassign: bool, yes: bool) -> None:
    """WRITE: set or clear an issue's assignee. Gated -- requires --yes.

    PUT /rest/api/2/issue/{key}/assignee with ``{"name": ...}`` -- Data Center
    keys the assignee by *username*; the Cloud `accountId` form does not apply.
    Clearing sends an explicit null, which the project's permission scheme may
    forbid (`Assignable user` / "allow unassigned issues")."""
    if unassign and assignee:
        raise click.ClickException("pass either --assignee or --unassign, not both.")
    if not unassign and not assignee:
        raise click.ClickException("pass --assignee <username> or --unassign.")
    _require_approval(
        yes,
        f"clear the assignee on {issue_key}"
        if unassign
        else f"assign {issue_key} to {assignee!r}",
    )
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = _write(
            client,
            connector,
            method="PUT",
            path=f"/rest/api/2/issue/{issue_key}/assignee",
            payload={"name": None if unassign else assignee},
            verb="assign issue",
            query_text=issue_key,
        )
    _emit_write_result(
        r,
        verb="assign issue",
        done=(
            f"Assignee cleared on {issue_key}."
            if unassign
            else f"{issue_key} assigned to {assignee}."
        ),
    )


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior search.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=click.IntRange(1), help="Row number within the result set.")
def view_result(ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior search (pure cache read)."""
    try:
        record = cache.get_row(ref, result_set, row - 1)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(record, indent=2))


if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
