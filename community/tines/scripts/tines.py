#!/usr/bin/env python3
"""tines CLI -- drives a Tines SOAR custom connector via the MCP api_proxy
tool (the ``/api/v1`` REST surface).

Subcommands:
  query          List stories (Tines' unit of automation -- what the SOC
                  calls a playbook). Server-side filters for tag, team and
                  free-text search. Emits grouped PSV with a ref for
                  drill-down, plus the tenant-wide match count so the agent
                  can judge scope before paging further.
  get-story      Hydrate one story by numeric id -- full detail card.
  list-agents    List the actions inside one story (the trigger-discovery
                  surface). Tines calls these "actions" now; the API still
                  says "agents". Webhook secrets are redacted.
  view-result    Re-print one row (full entity JSON) from a prior query.
  sample/lookup  KG edge-learning row sampling.
  refresh        Walk the stories/actions field catalog into the KG.
  ls             List this skill's Tines connectors' schema by UNIX path.
  grep           String-search this skill's connectors' KG catalog.

Auth is a single Tines API key, injected server-side as the
``Authorization: Bearer`` header from the connector's stored secret. Tines
also accepts the same key in an ``x-user-token`` header; Bearer is used here
because it is the shape the connector binding already expresses. This skill
is bound to one configured connector and resolves it automatically; you
don't pass a connector name.

Read-only: story execution is NOT exposed. Neither Send-to-Story
(``POST /api/v1/stories/:id/events``) nor webhook trigger
(``POST /webhook/<path>/<secret>``) is reachable from this CLI, so the key
needs no permission beyond reading stories. Execution belongs in a workflow
skill layered on top, where the approval gate lives.

Webhook secrets: the actions endpoint returns ``options.secret`` for every
WebhookAgent, which is the full auth for that webhook. Every action payload is
scrubbed of ``secret`` keys before it is rendered, cached, or uploaded to the
knowledge graph -- see ``_redact``.

Endpoint provenance, both documented:
  stories  https://www.tines.com/stories/docs/api/stories/list/
  actions  https://www.tines.com/stories/docs/api/stories/actions/list/

The actions endpoint is nested under Stories in the vendor's API navigation,
not at the top level, which is why an earlier pass here wrongly recorded it as
undocumented. Tines renamed the concept from "agent" to "action": the
documented path is ``/api/v1/actions``, while ``/api/v1/agents`` is the
pre-rename alias. This CLI calls the documented path and falls back to the
alias on a 404 for older self-hosted installs. The response envelope key and
the ``Agents::<Type>`` type strings are unchanged on both.

A 404 also carries a second meaning worth knowing: per Tines' auth docs, an
underprivileged API key hitting a resource it lacks permission for gets
``404: Not Found``, not a 401/403. A "missing" story may be a permissions
gap, not a wrong id.
"""

from __future__ import annotations

import copy
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

SKILL_NAME = "tines"
PACKAGE = "tines"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "tines.py view-result"

# Context-budget rules (mirror the SKILL.md). Tines' own `per_page` allows up
# to 500; these are this connector's tighter budget guards. Agents are capped
# harder than stories because a single complex story can hold 50+ agents and
# each agent payload runs 300-1500 bytes.
DEFAULT_LIMIT = 10
HARD_CAP = 50
AGENTS_DEFAULT_LIMIT = 10
AGENTS_HARD_CAP = 25

STORIES_PATH = "/api/v1/stories"
# Tines renamed "agents" to "actions"; /api/v1/actions is the documented path and
# /api/v1/agents is the pre-rename alias kept for older self-hosted installs. The
# response envelope key stays `agents` on both -- vendor backwards compatibility.
ACTIONS_PATH = "/api/v1/actions"
LEGACY_AGENTS_PATH = "/api/v1/agents"

RESOURCE_STORIES = "stories"
RESOURCE_AGENTS = "agents"
RESOURCES = (RESOURCE_STORIES, RESOURCE_AGENTS)

WEBHOOK_AGENT_TYPE = "Agents::WebhookAgent"

# Documented single-value `filter` values on GET /api/v1/stories. SEND_TO_STORY_ENABLED
# is the one that matters most here: it answers "can this story be triggered via
# Send-to-Story" straight from the story surface, in one call, rather than inferring
# it from a non-empty `entry_agent_ids` or walking each story's actions.
STORY_FILTERS = (
    "SEND_TO_STORY_ENABLED",
    "HIGH_PRIORITY",
    "API_ENABLED",
    "FAVORITE",
    "CHANGE_CONTROL_ENABLED",
    "CHANGE_CONTROL_DISABLED",
    "ENABLED",
    "DISABLED",
    "LOCKED",
)
REDACTED = "<secret-redacted>"

_JSON_HEADERS = {"Accept": "application/json"}


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


def _redact(value: Any) -> Any:
    """Recursively replace any ``secret`` key's value with a redaction marker.

    A WebhookAgent's ``options.secret`` is the entire auth for that webhook --
    ``https://<tenant>/webhook/<path>/<secret>`` needs nothing else. Since this
    connector is read-only, no caller ever has a legitimate need for the raw
    value, so it is scrubbed at the boundary rather than at render time: the
    redacted copy is what gets emitted, cached for `view-result`, sampled, and
    upserted into the knowledge graph. Redacting only in the PSV would still
    leave the live secret in the row cache and in any KG upload.

    Matches the key name case-insensitively and at any depth, so a nested or
    renamed-case ``Secret`` in a non-webhook agent's options is covered too.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() == "secret":
                out[k] = REDACTED if v is not None else None
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _story_query_params(
    tag: str,
    team_id: str,
    folder_id: str,
    search: str,
    story_filter: str,
    limit: int,
    page: int,
) -> dict[str, str]:
    """Build the query dict for GET /api/v1/stories.

    Server-side filters keep hundreds of irrelevant stories out of the context
    budget. Exactly one tag per call -- and note where that limit comes from:
    Tines' own ``tags`` parameter *is* an array and accepts several tag names,
    but the connector proxy serializes ``query`` as a string->string map before
    URL-encoding it, so a repeated ``tags[]=a&tags[]=b`` cannot be expressed
    through this transport, and a CSV under one ``tags=`` key would be matched
    as a single literal tag name (matching nothing). Multi-tag is therefore a
    client-side union here -- see ``_list_stories_union``.
    """
    params: dict[str, str] = {"per_page": str(min(limit, HARD_CAP))}
    if page > 1:
        params["page"] = str(page)
    if tag:
        params["tags"] = tag
    if team_id:
        params["team_id"] = team_id
    if folder_id:
        params["folder_id"] = folder_id
    if search:
        params["search"] = search
    if story_filter:
        params["filter"] = story_filter
    return params


def _describe_query(
    tags: tuple[str, ...],
    team_id: str,
    search: str,
    story_filter: str = "",
    folder_id: str = "",
) -> str:
    """Human-readable echo of the filter set, for the cache record and errors."""
    bits = []
    if tags:
        bits.append(f"tags={','.join(tags)}")
    if team_id:
        bits.append(f"team_id={team_id}")
    if folder_id:
        bits.append(f"folder_id={folder_id}")
    if search:
        bits.append(f"search={search}")
    if story_filter:
        bits.append(f"filter={story_filter}")
    return " ".join(bits)


def _unwrap(payload: Any, key: str, what: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull the row array and (optional) meta object out of a list response.

    Both list responses are ``{<key>: [...], meta: {...}}``. The key is
    ``stories`` for stories and -- see ``_list_agents`` on the rename --
    ``agents`` for actions. ``meta`` is tolerated as absent so a build that
    omits it degrades to "no count reported" rather than an exception.
    """
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"{what}: expected an object with a {key!r} array, got "
            f"{type(payload).__name__}"
        )
    rows = payload.get(key)
    if not isinstance(rows, list):
        raise click.ClickException(f"{what}: expected payload to have a {key!r} array")
    meta = payload.get("meta")
    return [r for r in rows if isinstance(r, dict)], meta if isinstance(meta, dict) else {}


def _list_stories(
    client: mcp.MCPClient,
    connector: str,
    tag: str,
    team_id: str,
    folder_id: str,
    search: str,
    story_filter: str,
    limit: int,
    page: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One GET /api/v1/stories call -> (rows, meta). At most one tag."""
    params = _story_query_params(
        tag, team_id, folder_id, search, story_filter, limit, page
    )
    r = proxy.dispatch(
        client,
        connector,
        method="GET",
        path=STORIES_PATH,
        query=params,
        headers=_JSON_HEADERS,
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="tines-rest",
            verb="list stories",
            connector=connector,
            package=PACKAGE,
            query=_describe_query(
                (tag,) if tag else (), team_id, search, story_filter, folder_id
            ),
            response=r,
            candidates_path=[RESOURCE_STORIES],
            client=client,
        )
    rows, meta = _unwrap(_parse_json(r.body, "list stories"), "stories", "list stories")
    # Story objects carry no secret field today, but redacting here too keeps the
    # invariant flat and auditable -- nothing leaves this CLI unredacted -- rather
    # than resting on a per-endpoint judgment that could rot if Tines adds a field.
    return [_redact(copy.deepcopy(row)) for row in rows[:limit]], meta


def _list_stories_union(
    client: mcp.MCPClient,
    connector: str,
    tags: tuple[str, ...],
    team_id: str,
    folder_id: str,
    search: str,
    story_filter: str,
    limit: int,
    page: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """List stories across zero or more tags, unioning client-side.

    Tines' ``tags`` parameter accepts an array; this transport cannot send a
    repeated key (see ``_story_query_params``), so N tags means N calls deduped
    by story id. The returned meta carries ``count``/``pages`` verbatim from the
    API only in the single-call case; for a union those numbers describe one
    tag's result set, not the union, so they are replaced with a ``union_of``
    marker and the caller reports the union size instead of claiming a
    tenant-wide total it doesn't have.
    """
    if len(tags) <= 1:
        return _list_stories(
            client, connector, tags[0] if tags else "", team_id, folder_id,
            search, story_filter, limit, page,
        )

    seen: dict[Any, dict[str, Any]] = {}
    for tag in tags:
        rows, _ = _list_stories(
            client, connector, tag, team_id, folder_id, search, story_filter,
            limit, page,
        )
        for row in rows:
            row_id = row.get("id")
            key = row_id if row_id is not None else json.dumps(row, sort_keys=True)
            seen.setdefault(key, row)
    return list(seen.values())[:limit], {"union_of": len(tags)}


def _list_agents(
    client: mcp.MCPClient,
    connector: str,
    story_id: str,
    agent_type: str,
    limit: int,
    story_mode: str = "",
) -> list[dict[str, Any]]:
    """GET /api/v1/actions?story_id=<id>, redacted.

    Naming, because it is genuinely confusing: Tines renamed this concept from
    "agent" to "action" in the product and docs, but kept the wire format for
    backwards compatibility. So the documented path is ``/api/v1/actions``, the
    response envelope key is still ``agents``, and type values are still
    ``Agents::<Type>`` (the Trigger -> Condition rename explicitly kept
    ``Agents::TriggerAgent`` to avoid breaking existing stories). This function
    keeps the internal ``agent`` vocabulary to match the payload it parses.

    ``story_id`` is always sent: without it Tines returns every action in the
    tenant, which is both a context-budget and a relevance problem.
    ``action_type`` filters server-side, so a webhook-only lookup does not
    transfer the whole action set and then discard most of it.

    Falls back to the legacy ``/api/v1/agents`` path on a 404. Older
    self-hosted installs predate the rename, and the vendor release notes don't
    pin a version for when ``/api/v1/actions`` landed, so the fallback is
    version-detection by probe rather than by assertion.
    """
    # Clamp here as well as at the click layer: this helper is also called by the
    # KG sampling paths, and the stories path clamps internally for the same reason.
    limit = min(limit, AGENTS_HARD_CAP)
    query: dict[str, str] = {"story_id": story_id, "per_page": str(limit)}
    if agent_type:
        query["action_type"] = agent_type
    if story_mode:
        query["story_mode"] = story_mode

    r = proxy.dispatch(
        client, connector, method="GET", path=ACTIONS_PATH,
        query=query, headers=_JSON_HEADERS,
    )
    path_used = ACTIONS_PATH
    if r.status_code == 404:
        legacy = proxy.dispatch(
            client, connector, method="GET", path=LEGACY_AGENTS_PATH,
            query=query, headers=_JSON_HEADERS,
        )
        # Only adopt the legacy result if it actually worked. If both 404, report
        # against the documented path -- and note a 404 here is ambiguous: per
        # Tines' auth docs an underprivileged key also gets 404, so this may be a
        # permissions gap rather than a missing endpoint or a bad story id.
        if legacy.status_code < 400:
            r, path_used = legacy, LEGACY_AGENTS_PATH

    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="tines-rest",
            verb="list actions",
            connector=connector,
            package=PACKAGE,
            query=f"story_id={story_id} via {path_used}",
            response=r,
            candidates_path=[RESOURCE_AGENTS],
            client=client,
        )
    # Envelope key is `agents` on both paths -- vendor backwards compatibility.
    rows, _ = _unwrap(_parse_json(r.body, "list actions"), "agents", "list actions")
    if agent_type:
        # Belt-and-braces: the legacy path has no server-side action_type, and a
        # server that ignores an unknown param would otherwise return everything.
        rows = [a for a in rows if a.get("type") == agent_type]
    return [_redact(copy.deepcopy(a)) for a in rows[:limit]]


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
    An empty result prints an explicit 0-row line (never a blank line) so the
    agent doesn't mistake success-with-no-data for a failed call and retry."""
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


class _TinesSchemaProvider:
    """SchemaProvider for tines: {stories, agents} -> field.

    Tines exposes no field-definition endpoint (no equivalent of XSOAR's
    ``/incidentfields``), so leaf fields are sampled from a live row rather
    than enumerated authoritatively -- hence LEAF_DISCOVERY_SAMPLED = True.
    Agent rows are redacted before their keys are walked; the ``secret`` key
    still appears in the catalog as a field name, which is correct (it is a
    real field), while its value never leaves the connector.
    """

    PACKAGE = PACKAGE
    PLATFORM = "Tines"
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
            return list(RESOURCES)
        if len(tiers) == 1 and tiers[0] in RESOURCES:
            return self._discover_fields(connector, tiers[0])
        return []

    def _discover_fields(self, connector: str, resource: str) -> list[str]:
        """Union the top-level keys across a small sample of rows.

        One row is not enough: `send_to_story_enabled` is documented as
        present-or-absent per story, and agent shapes vary by type, so a
        single-row sample would miss fields depending on which row came back
        first.
        """
        rows = self._sample(connector, resource)
        names: set[str] = set()
        for row in rows:
            names.update(k for k in row if isinstance(k, str))
        return sorted(names)

    def _sample(self, connector: str, resource: str) -> list[dict[str, Any]]:
        if resource == RESOURCE_STORIES:
            rows, _ = _list_stories(self._client, connector, "", "", "", "", "", 10, 1)
            return rows
        stories, _ = _list_stories(self._client, connector, "", "", "", "", "", 3, 1)
        agents: list[dict[str, Any]] = []
        for story in stories:
            story_id = story.get("id")
            if story_id is None:
                continue
            agents.extend(
                _list_agents(self._client, connector, str(story_id), "", AGENTS_DEFAULT_LIMIT)
            )
        return agents


_EXAMPLES = """
Examples:

\b
  tines.py query --limit 25
  tines.py query --tag remediation --limit 50
  tines.py query --search "isolate host"
  tines.py query --team-id 42 --page 2
  tines.py query --filter SEND_TO_STORY_ENABLED --limit 25
  tines.py get-story --id 12345
  tines.py list-agents --story-id 12345
  tines.py list-agents --story-id 12345 --webhooks-only
  tines.py view-result --ref <ref> --row 1
  tines.py grep --pattern webhook
  tines.py ls --path /
"""


@click.group(epilog=_EXAMPLES)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Drive a Tines SOAR connector. Run `<command> --help` for details."""
    ctx.obj = {}


@cli.command(name="query")
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help="Story tag to filter on, server-side. Exact match, not substring; "
    "stories with no tags cannot be filtered this way. Repeatable: Tines' own "
    "`tags` parameter takes an array, but this connector's proxy transport "
    "cannot send a repeated key, so N tags becomes N calls unioned client-side "
    "(each capped at --limit) -- and --page cannot be combined with more than "
    "one tag.",
)
@click.option(
    "--team-id",
    default="",
    help="Numeric team id -- Tines tenants are partitioned by team. Filters server-side.",
)
@click.option(
    "--folder-id",
    default="",
    help="Numeric folder id -- stories are organized into folders within a team. "
    "Filters server-side.",
)
@click.option(
    "--search",
    default="",
    help="Free-text server-side search against story names (names only -- the "
    "vendor's `search` does not cover descriptions).",
)
@click.option(
    "--filter",
    "story_filter",
    type=click.Choice(STORY_FILTERS, case_sensitive=True),
    default=None,
    help="Vendor single-value story filter. SEND_TO_STORY_ENABLED is the documented "
    "way to ask which stories accept Send-to-Story; ENABLED/DISABLED separates live "
    "stories from paused ones. Single-value only -- the API takes one, not a set.",
)
@click.option(
    "--limit",
    default=DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, HARD_CAP),
    help=f"Max rows, sent as per_page (hard cap {HARD_CAP}; Tines itself allows up to 500).",
)
@click.option(
    "--page",
    default=1,
    show_default=True,
    type=click.IntRange(1),
    help="1-indexed page. Advance deliberately after reading the match count; "
    "do not sweep every page unless the user asked for an exhaustive list.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Also write the result rows to FILE in the verifiable upload-samples format.",
)
def query(
    tags: tuple[str, ...],
    team_id: str,
    folder_id: str,
    search: str,
    story_filter: str | None,
    limit: int,
    page: int,
    output_path: str | None,
) -> None:
    """List stories; emits grouped PSV with a ref for drill-down.

    A story is Tines' unit of automation. There is no /api/v1/playbooks
    endpoint -- "playbook" is a SOC-friendly rename, "story" is the API's
    word.
    """
    if len(tags) > 1 and page > 1:
        raise click.UsageError(
            "--page cannot be combined with more than one --tag: a multi-tag "
            "result is a client-side union of separate queries, so page N of "
            "the union is not a thing Tines can return. Query one tag at a "
            "time when you need to page."
        )
    story_filter = story_filter or ""
    query_text = _describe_query(tags, team_id, search, story_filter, folder_id)
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows, meta = _list_stories_union(
            client, connector, tags, team_id, folder_id, search, story_filter,
            limit, page,
        )
    ref = _emit_rows(
        rows,
        command="query",
        connector=connector,
        query=query_text,
        catalog_path=[RESOURCE_STORIES],
    )
    # Report scope so the agent can judge whether to narrow, without paging
    # blindly. A single call carries an exact tenant-wide `count`; a union does
    # not, so say what the number actually is rather than overstating it.
    shown = len(rows)
    if meta.get("union_of"):
        click.echo(
            f"\n{shown} shown, unioned across {meta['union_of']} tag queries "
            f"(each capped at --limit {limit}). No tenant-wide total is "
            f"available for a multi-tag union -- query one tag for an exact count."
        )
    else:
        count, pages = meta.get("count"), meta.get("pages")
        if isinstance(count, int):
            tail = f" across {pages} page(s)" if isinstance(pages, int) else ""
            click.echo(
                f"\n{shown} shown; {count} story(ies) match this filter{tail}. "
                f"Narrow with --tag/--team-id/--folder-id/--filter/--search "
                f"rather than sweeping pages."
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


@cli.command(name="get-story")
@click.option("--id", "story_id", required=True, help="Numeric story id, e.g. 12345.")
def get_story(story_id: str) -> None:
    """Hydrate one story by id and print the full JSON detail card.

    Read `entry_agent_ids` (non-empty) or `send_to_story_enabled: true` to tell
    whether this one story supports Send-to-Story. Absence of the flag is not
    proof of absence of support -- fall back to entry_agent_ids. For the
    fleet-level question ("which stories accept Send-to-Story") prefer
    `query --filter SEND_TO_STORY_ENABLED`, which is the documented answer and
    one call instead of N.
    """
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        r = proxy.dispatch(
            client,
            connector,
            method="GET",
            path=f"{STORIES_PATH}/{story_id}",
            headers=_JSON_HEADERS,
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="tines-rest",
                verb="get story",
                connector=connector,
                package=PACKAGE,
                query=story_id,
                response=r,
                candidates_path=None,
                client=client,
            )
        story = _parse_json(r.body, "get story")
    click.echo(json.dumps(_redact(story), indent=2))


@cli.command(name="list-agents")
@click.option("--story-id", required=True, help="Numeric story id to enumerate agents for.")
@click.option(
    "--webhooks-only",
    is_flag=True,
    default=False,
    help=f"Filter to {WEBHOOK_AGENT_TYPE} -- the external trigger actions. Sent "
    "server-side, so the whole action set is never transferred just to discard most "
    "of it. A story with zero webhook actions is internal, called by another story.",
)
@click.option(
    "--type",
    "agent_type",
    default="",
    help="Filter to an exact action type, e.g. Agents::SendToStoryAgent. Sent as the "
    "server-side `action_type` parameter. Type values keep the legacy `Agents::` "
    "prefix even though the product now calls these actions. Ignored when "
    "--webhooks-only is set.",
)
@click.option(
    "--story-mode",
    type=click.Choice(["LIVE", "TEST"], case_sensitive=True),
    default=None,
    help="Which copy of the story to read actions from. Only meaningful alongside "
    "--story-id; omit to let Tines choose its default.",
)
@click.option(
    "--limit",
    default=AGENTS_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, AGENTS_HARD_CAP),
    help=f"Max rows, sent as per_page (hard cap {AGENTS_HARD_CAP}). A complex story "
    "can hold 50+ actions.",
)
def list_agents(
    story_id: str,
    webhooks_only: bool,
    agent_type: str,
    story_mode: str | None,
    limit: int,
) -> None:
    """List the agents inside one story; emits grouped PSV with a ref.

    This is the trigger-discovery surface: for a WebhookAgent, `options.path`
    is the webhook path component. `options.secret` is redacted -- it is the
    full auth for that webhook, and this connector is read-only, so nothing
    downstream needs it.
    """
    wanted = WEBHOOK_AGENT_TYPE if webhooks_only else agent_type
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _list_agents(
            client, connector, story_id, wanted, limit, story_mode or ""
        )
    _emit_rows(
        rows,
        command="list-agents",
        connector=connector,
        query=f"story_id={story_id}" + (f" type={wanted}" if wanted else ""),
        catalog_path=[RESOURCE_AGENTS],
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
    "--row", required=True, type=click.IntRange(1), help="Row number within the result set."
)
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
    """KG sample/lookup over stories and agents, in the canonical row shape
    upload-samples upserts. A path that is not a known resource returns [].

    Stories support a `column`/`values` lookup by mapping it onto whichever
    server-side filter matches the column (tag, team, folder), falling back to
    free-text search. Actions are only reachable per-story, so a lookup keyed on
    story_id is the only supported form.
    """
    if len(segments) != 1 or segments[0] not in RESOURCES:
        return []

    if segments[0] == RESOURCE_STORIES:
        tags: tuple[str, ...] = ()
        team_id = ""
        folder_id = ""
        search = ""
        if column is not None:
            if not values:
                return []
            if column in ("tag", "tags", "tag_list"):
                tags = tuple(values)
            elif column == "team_id":
                team_id = values[0]
            elif column == "folder_id":
                folder_id = values[0]
            else:
                # No server-side filter for this column; fall back to
                # free-text search rather than silently ignoring the filter.
                search = values[0]
        rows, _ = _list_stories_union(
            client, connector, tags, team_id, folder_id, search, "",
            min(limit, HARD_CAP), 1,
        )
        return rows

    if column in ("story_id", "story") and values:
        return _list_agents(
            client, connector, str(values[0]), "", min(limit, AGENTS_HARD_CAP)
        )
    # Unkeyed agent sampling: walk a few stories to get a representative mix
    # of agent shapes rather than every agent in the tenant.
    stories, _ = _list_stories(client, connector, "", "", "", "", "", 3, 1)
    out: list[dict[str, Any]] = []
    for story in stories:
        story_id = story.get("id")
        if story_id is None:
            continue
        out.extend(
            _list_agents(client, connector, str(story_id), "", min(limit, AGENTS_HARD_CAP))
        )
        if len(out) >= limit:
            break
    return out[:limit]


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _TinesSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Tines",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Tines connectors this skill owns; ``/<connector>/stories/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Tines connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill\ninto the tiers below. To search the catalog by name instead of walking\nit tier by tier, use the ``grep`` subcommand.\n\nHierarchy: {stories, agents} -> Field. Tines has no field-definition\nendpoint, so fields are sampled from live rows.",
    refresh_docstring="Walk the stories/agents field catalog in real time and commit catalog tiers to the KG.\n\nThe only ``tines`` command that hits the connector backend for schema\ndiscovery; ``ls`` reads exclusively from the KG cache. Walks are\nresumable per-tier; stale schema items are pruned at the tier they\ndisappear from. Agent rows are redacted before their keys are walked.",
    grep_docstring="String-search this skill's Tines connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its\nLLM-assigned summary, and its tags. Reads the persisted knowledge graph\n(populated by `refresh` + the kg-learner describe pass), not the live API.\nDefault match is a literal case-insensitive substring; use --regex for a\nPOSIX regex or --fuzzy for typo-tolerant trigram matching. To search every\nconnector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact filter set (--tag/--team-id/--search) the samples came from -- must match the source query.",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by `tines.py query --output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /stories/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query`) or\n`--from-file FILE`. Exactly one is required. Both enforce a read-before-write\ncheck: the agent must have queried these exact rows with this exact\nquery, the export cannot exceed the upload limit, and a content hash\nmust match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP\ntool; the server resolves --catalog-path tier-by-tier (lazily creating\ncatalog entries) and walks each sample's nested JSON keys into child nodes\nunder the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
