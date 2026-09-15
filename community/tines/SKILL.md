---
name: tines
description: "PREFER THIS SKILL for ad-hoc / exploratory queries against a Tines SOAR tenant via the `tines.py` CLI: 'list playbooks', 'show me stories tagged X', 'what stories are in this tenant', 'tell me about story 12345', 'what agents are in story X', 'which stories have a webhook trigger', 'find the entry point for story X'. Data-retrieval cookbook -- composes the call, renders a table/card, and stops. Read-only: story execution (Send-to-Story and webhook trigger) is NOT exposed, so the API key needs no permission beyond reading stories. Tines SOAR ONLY -- for other SOAR products (XSOAR, Swimlane, Splunk SOAR) use the relevant connector. The unit of automation is a `story`, not a `playbook`: there is no /api/v1/playbooks endpoint. Tines renamed `agents` to `actions`, but the API still returns them under an `agents` key with `Agents::` type prefixes."
version: "0.2.0"
status: draft
paired_connector_type: tines
---

# Tines

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.2.0-blue)

> **v0.2.0** -- 2026-08-12 -- Migrated the standalone `tines-reference` cookbook into an importable community connector type: Python CLI (`scripts/tines.py`), `connector.json` binding (API key / `Authorization: Bearer`, skip-TLS option), and a provisioning guide. Scope is **read-only** retrieval over stories and actions; the reference's two execution surfaces (Send-to-Story, webhook trigger) are deliberately not carried over -- see *Guardrails*. Call shapes and the story/agent field tables trace to the live-authored `tines-reference` v0.1.0, which was seeded from real discovery against a Tines tenant. The bundle itself is **draft**: built and validated locally against the connector validator, not yet run against a live tenant.
>
> **Reference retired into this bundle.** `references/tines-reference/` is gone; this is now the only Tines skill in the repo. Its last genuinely-unique material was harvested first: tag matching is **case-sensitive** (a real gap -- a case mismatch returns zero rows with nothing to explain it), the per-token rate-limit ceiling, the endpoints this CLI has no subcommand for, and the fact that **response headers never reach the CLI** -- which quietly rules out self-throttling on `X-RateLimit-*`, `Link:`-header pagination, and, most consequentially, reporting back the `X-Tines-Event-Id` of any story a future write path fires. See *Out of reach from this CLI*. What was left behind was either already carried here, execution-only (out of scope by design), or factually wrong; the archived zip keeps a copy of all of it either way.
>
> Call shapes were then spot-checked against Tines' published API docs, which corrected five things the inherited reference had wrong: the API-key model is four *categories* (Personal / Service / Team / Tenant owner) rather than a read scope plus a "send to story" permission; the key is minted under **Settings -> Access & security -> API keys**, not "Settings -> API Tokens"; `per_page` maxes at **500**, not 200; `search` matches **story names only**, not descriptions; and -- the consequential one -- **an underprivileged key gets `404`, not `401`/`403`**, so a "missing" story may be a permissions gap. The docs also surfaced a documented `filter` parameter (including `SEND_TO_STORY_ENABLED`) and a `folder_id` scope filter, both now exposed. It also resolved an endpoint question: `GET /api/v1/agents` is the **pre-rename alias** for the documented `GET /api/v1/actions` (Tines renamed "agents" to "actions" while keeping the `agents` envelope key and `Agents::` type prefixes for compatibility). An earlier pass here recorded that endpoint as undocumented, which was wrong -- its docs are nested under Stories, not at the top level of the API navigation. The CLI now calls the documented path, falls back to the alias on a 404 for older self-hosted installs, and uses the documented server-side `action_type` filter instead of filtering types client-side.

One CLI: `scripts/tines.py`. Run `scripts/tines.py --help` for the subcommand list. This is a **data-retrieval cookbook**: read the question, compose the call, render the result, stop. No phase gates, no orchestration, no write-back.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. This is the only Tines skill in the repo -- the former `tines-reference` cookbook has been retired into it, with its distributable archived at `dist/references/_superseded/tines-reference.zip`.

**Vocabulary, and the mistake to avoid.** Tines' unit of automation is a **story**. "Playbook" is a SOC-friendly rename (the retired bundled Crogl plugin used it); the API does not. `GET /api/v1/playbooks` 404s. Likewise Tines is on **v1 only** -- every `/api/v2/*` path 404s -- and `runs` is not a top-level resource, so `GET /api/v1/runs` 404s too. When the user says "playbook", read "story" and carry on; don't surface the rename as a correction.

## Fast Path

For a simple "what remediation stories do we have" request, run the CLI directly:

```bash
python3 <cli_path> query --tag remediation --limit 25
```

## Subcommands

| Subcommand | Purpose |
|---|---|
| `query` | List stories (`GET /api/v1/stories`), with server-side `--tag` / `--team-id` / `--folder-id` / `--search` / `--filter` filters. Returns grouped PSV + a result `ref`, then the tenant-wide match count. |
| `get-story` | Hydrate one story by numeric id -- full JSON detail card. |
| `list-agents` | List the actions inside one story (`GET /api/v1/actions?story_id=`) -- the trigger-discovery surface. `--webhooks-only` / `--type` filter server-side; `--story-mode` picks LIVE vs TEST. Secrets redacted. |
| `view-result` | Re-print one cached row (full entity JSON) from a prior query. |
| `sample` / `lookup` | KG edge-learning row sampling over `stories` and `agents` (the KG resource names keep the wire vocabulary). |
| `ls` / `grep` / `refresh` / `upload-samples` | Knowledge-graph catalog surface. |

Read-only by design. There are no execution subcommands -- see *Guardrails*.

## Auth Recap

The connector stores `host` (the bare Tines tenant FQDN, e.g. `acme.tines.com`, bound into `base_url` as `https://<host>`) and `api_key` (secret). crogld injects it as **`Authorization: Bearer <key>`** server-side. The agent must never construct the header, prompt the user for the key, or log it.

- The key is created in Tines under **Settings -> Access & security -> API keys** ([vendor docs](https://www.tines.com/stories/docs/api/authentication/)). Tines has four key categories: **Personal** (inherits that user's access), **Service** (service-account-linked, grantable tenant permissions), **Team** (role-based access to one team), and **Tenant owner** (full owner access to everything). Use a **Team** or **Service** key; never a Tenant owner key. There is no read/write scope toggle and no "send to story" permission to withhold -- access is governed by the key's category and the permissions granted to it.
- Tines accepts the same key in either an `Authorization: Bearer` header or an **`x-user-token`** header. This connector binds the Bearer form.
- Tines does not document key expiry or a rotation procedure, so treat a sudden `401` as revoked-or-wrong rather than assuming a lapsed TTL.
- The CLI adds only `Accept: application/json` to each call.

**TLS:** Tines Cloud presents a public CA-signed certificate and needs nothing. Self-hosted tenants behind a private CA may need the **Skip TLS verification** option enabled when creating the connector (off by default). A TLS handshake / certificate-verify error is install-time configuration, not a query problem -- surface it and stop.

## Context Budget

1. **Default limit is 10 rows** for `query`; the CLI sends `per_page=10`. Hard cap is **50** (`click.IntRange(1, 50)`) even if the user says "all" / "everything" -- Tines itself allows `per_page` up to 500.
2. **`query` prints the exact tenant-wide match count** from the response `meta` after the table. Read it before deciding whether to page. Enterprise tenants can hold hundreds of stories; narrow with `--tag` / `--team-id` / `--folder-id` / `--filter` rather than sweeping `--page`.
3. **Actions are capped harder: default 10, hard cap 25.** A complex story can hold 50+ actions and each payload runs 300-1500 bytes. Use `--webhooks-only` when the question is "how is this story triggered" -- that is the common case and it cuts the payload sharply.
4. **`--tag` is exact match, not substring, and case-sensitive.** `remediation` and `Remediation` are different tags, so a case mismatch returns zero rows with no error to explain it -- check the case against a story's `tag_list` before concluding a tag is unused. Stories with no tags cannot be filtered server-side at all. Tines' own `tags` parameter *does* take an array of tag names -- the one-tag-per-call limit is **ours, not the vendor's**: the connector proxy serializes the query as a string->string map, so a repeated `tags[]=a&tags[]=b` can't be sent through this transport. Repeating `--tag` therefore makes N calls that the CLI unions client-side, each capped at `--limit`. A multi-tag union has **no exact tenant-wide count** (the CLI says so in its footer instead of inventing one), and `--page` is rejected alongside more than one tag -- page N of a client-side union isn't a thing the API can return. Query one tag at a time when you need either an exact count or pagination.
5. After rendering, drop the raw JSON from your reasoning; use `view-result --ref <ref> --row N` to re-hydrate a single row instead of re-querying.
6. Don't re-discover within a session -- the catalog is stable for the conversation. Reference the prior `ref`.

## Data model

**Story** (`GET /api/v1/stories`, response `{stories: [...], meta: {...}}`):

| Field | Notes |
|---|---|
| `id` | Stable across renames. Use for `?story_id=`. |
| `name` | Renameable in the UI without breaking ids. |
| `description` | Often empty; some teams put the inputs schema here as plain text. |
| `tag_list` | Server-side filterable via `--tag`. |
| `team_id` | Tenants are partitioned by team; server-side filterable. |
| `entry_agent_ids` | Agents that can serve as entry points. Empty array -> no Send-to-Story support, webhook only. |
| `send_to_story_enabled` | Explicit flag **when present**. Absence is not proof of absence -- fall back to `entry_agent_ids` being non-empty. |
| `disabled` | True -> story accepts events but agents are paused. Flag these; don't present them as available. |
| `mode` | `LIVE` / `TEST` / `STAGING`. Production stories are `LIVE`. |
| `published` | Whether the story is shared as a template. Not a runtime flag -- ignore for catalog purposes. |
| `created_at` / `updated_at` | ISO UTC. |

`meta` carries `current_page` (1-indexed), `previous_page`, `next_page`, `next_page_number`, `per_page`, `pages`, and `count` (exact total for the filter). `next_page` being null is the cheapest "no more pages" signal.

**Documented server-side query parameters** ([vendor docs](https://www.tines.com/stories/docs/api/stories/list/)), and which the CLI exposes:

| Parameter | CLI | Notes |
|---|---|---|
| `per_page` / `page` | `--limit` / `--page` | Vendor max `per_page` is 500; this CLI caps at 50. |
| `team_id` | `--team-id` | |
| `folder_id` | `--folder-id` | Folders subdivide a team. |
| `tags` | `--tag` | Vendor takes an array; this transport can't send one (see *Context Budget* 4). |
| `search` | `--search` | Matches **story names only**, not descriptions. |
| `filter` | `--filter` | Single value from: `SEND_TO_STORY_ENABLED`, `HIGH_PRIORITY`, `API_ENABLED`, `FAVORITE`, `CHANGE_CONTROL_ENABLED`, `CHANGE_CONTROL_DISABLED`, `ENABLED`, `DISABLED`, `LOCKED`. |
| `order` | not exposed | `NAME`, `NAME_DESC`, `RECENTLY_EDITED`, `LEAST_RECENTLY_EDITED`, and several count-based orderings. |
| `include_live_activity` | not exposed | Adds run-count / token-usage metrics per story. |

**Action** (`GET /api/v1/actions?story_id=<id>`, response `{agents: [...], meta: {...}}` -- the envelope key stays `agents`, see *Endpoint provenance*). Types worth knowing:

| Type | Role |
|---|---|
| `Agents::WebhookAgent` | External trigger. `options.path` is the webhook path component; `options.secret` is redacted by this CLI. |
| `Agents::EventTransformationAgent` | Re-shapes events; sometimes the de-facto entry agent when a story uses Send-to-Story with no webhook. |
| `Agents::SendToStoryAgent` | Cross-story dispatch -- the source story chains into another via the API. |
| `Agents::HTTPRequestAgent` | Outbound HTTP. Common as the last agent in an approval-pair story, posting the Slack approval card. |
| `Agents::TriggerAgent` | Conditional branching. Internal control flow, not an entry point. |
| `Agents::EmailAgent` / `Agents::SlackAgent` | Notification. Not entry points. |

A story with **zero** webhook agents is internal -- called by another story, not externally triggerable. Say so rather than reporting "no trigger found".

## Common recipes

| User phrasing | Command |
|---|---|
| "What stories/playbooks do we have" | `query --limit 25` (then read the match count) |
| "Stories tagged remediation" | `query --tag remediation --limit 50` |
| "Find the isolate-host story" | `query --search "isolate host"` |
| "What's in team 42" | `query --team-id 42` |
| "Tell me about story 12345" | `get-story --id 12345` |
| "How is story 12345 triggered" | `list-agents --story-id 12345 --webhooks-only` |
| "What agents are in story 12345" | `list-agents --story-id 12345` |
| "Which stories support Send-to-Story" | `query --filter SEND_TO_STORY_ENABLED` -- the documented answer; prefer it over inferring from `entry_agent_ids` |
| "Does story 12345 support Send-to-Story" | `get-story --id 12345` -> `send_to_story_enabled: true`, or `entry_agent_ids` non-empty as the fallback |
| "Which stories are live vs paused" | `query --filter ENABLED` / `query --filter DISABLED` |
| "What inputs does story 12345 take" | `list-agents --story-id 12345 --webhooks-only` -> `options.schema` when present; else the story `description`; else ask the user |

### The two-story approval pattern -- detect, don't fabricate

Tines models manual approval as **two stories**, not one story with a flag:

- **Story A -- "\<Action\> Request"** -- webhook-triggered, posts a Slack approval prompt.
- **Story B -- "\<Action\> Execute"** -- webhook-triggered, performs the action; invoked by Story A's Slack approval button.

Detection heuristics, most to least reliable:

1. **Name-pair match** -- A contains "Request" / "Propose" / "Approval"; B is the same prefix with "Execute" / "Apply" / "Do" / "Action".
2. **Tag match** -- A tagged `requires-approval`, `manual-approval`, or `approval-gate`.
3. **Agent-graph match** -- A's last agent is a Slack `Agents::HTTPRequestAgent` posting an interactive message that references B.

When you detect a pair, present Story A as the user-facing entry and treat Story B as internal. The retired bundled plugin's tag-based detection misses this; name-pair matching holds up better in real tenants. Do **not** invent a pair that the data doesn't show.

## Rendering

**Stories (from `query`)** -- default columns: `id` · `name` · `description` (truncate at 60) · `tag_list` (csv) · `mode` · `disabled`.

**Agents (from `list-agents`)** -- default columns: `id` · `name` · `type` · `options.path`. The `secret` value is already `<secret-redacted>` in the payload.

Truncation: ≤25 stories or ≤10 agents render inline; beyond that show the first N + "(M more, ask to filter)". After rendering, drop the raw JSON from reasoning.

## Endpoint provenance and the agent/action rename

Both endpoints this skill uses are vendor-documented:

| | Path | Docs |
|---|---|---|
| Stories | `GET /api/v1/stories` | [Stories: List](https://www.tines.com/stories/docs/api/stories/list/) |
| Actions | `GET /api/v1/actions` | [Actions: List](https://www.tines.com/stories/docs/api/stories/actions/list/) |

**Tines renamed this concept from "agent" to "action"**, and the rename is half-applied in a way that will confuse you if you don't know it:

- The **documented path is `/api/v1/actions`**. `/api/v1/agents` is the pre-rename alias.
- The **response envelope key is still `agents`** on both paths.
- **Type values still carry the `Agents::` prefix** -- `Agents::WebhookAgent`, `Agents::HTTPRequestAgent`. When Tines renamed the Trigger action to Condition action (announced March 2026), they explicitly kept `Agents::TriggerAgent` on the wire to avoid breaking existing stories.
- The Actions docs are nested **under Stories** in the vendor's API navigation, not at the top level.

So: say "action" to the user, expect `agents` and `Agents::` on the wire, and don't "fix" the mismatch.

The CLI calls `/api/v1/actions` and falls back to `/api/v1/agents` on a 404, because older self-hosted installs predate the rename and the vendor release notes don't pin a version for when the new path landed. A non-404 error does not trigger the fallback probe.

### Documented action-listing parameters

| Parameter | CLI | Notes |
|---|---|---|
| `story_id` | `--story-id` | Always sent. Without it Tines returns every action in the tenant. |
| `action_type` | `--type` / `--webhooks-only` | Server-side type filter, so a webhook lookup never transfers the full action set. Takes a fully qualified `Agents::<Type>`. |
| `story_mode` | `--story-mode` | `LIVE` or `TEST`; only meaningful with `story_id`. |
| `per_page` | `--limit` | `page` is not exposed for actions; the per-story action count is small enough that the cap suffices. |
| `team_id`, `group_id`, `draft_id` | not exposed | |
| `include_live_activity` | not exposed | Adds `pending_action_runs_count`. |

Action objects also carry **`sources` and `receivers`** (arrays of action ids). Those are the story's internal graph edges, and they're the reliable way to confirm the agent-graph heuristic in the approval-pair section below rather than guessing from names.

## Out of reach from this CLI

Kept here so the next person extending this connector doesn't rediscover it. None of the below is callable today; don't try.

**Endpoints with no subcommand.** These exist on the Tines API but this bundle exposes no way to call them. Adding one means a new subcommand, not a creative use of an existing one:

| Path | What it gives you |
|---|---|
| `GET /api/v1/teams` | Team list, for resolving a `--team-id` on a multi-team tenant. |
| `GET /api/v1/stories/<id>/runs` | Run history. Ids and status only, not event payloads. |
| `GET /api/v1/events/<id>` | Event detail, keyed by an event id from a trigger call. |
| `GET /api/v1/notes` | Free-form author annotations; sometimes holds a story's inputs schema. |
| `GET /api/v1/stories/<id>/export` | Full story JSON, action graph and options included. Large. |

**Response headers are invisible to this connector.** The proxy returns a status code and a body, nothing else. Three consequences worth knowing, because each looks like a bug from the outside:

- `X-RateLimit-*` can't be read, so there's no self-throttling (see the 429 entry below).
- Tines' `Link:` header offers RFC 5988 `next`/`prev`/`last` pagination, but the CLI can't see it and constructs `?page=N` from `--page` instead.
- **This is a real constraint on any future execution support.** Send-to-Story and webhook triggers return `X-Tines-Event-Id` and `X-Tines-Status` in *headers*, which this transport drops. A write path built on the current proxy could fire a story but could not report back which run it started. That needs solving before execution is worth building, not after.

**Execution material.** The retired reference's worked Send-to-Story and webhook examples, its 504 async-timeout handling, and its execution-specific failure modes are archived in `dist/references/_superseded/tines-reference.zip`. Read them there if you pick up execution; note that the archive also carries the factual errors listed in its own banner.

## Error Handling

Distinct error classes, distinct fixes -- don't conflate them:

- **401 Unauthorized** -- the API key is invalid or revoked. Surface and stop; **never retry with the same key**. Prompt the operator to reissue it under Settings -> Access & security -> API keys and update the connector. (Credentials are not validated at connector-create time -- a bad key first surfaces here, on the first real call. Tines does not document key expiry, so don't diagnose this as a lapsed TTL.)
- **404 -- read this before concluding "not found".** Tines documents that *an underprivileged API key hitting a resource it lacks permission for returns `404: Not Found`*, not a 401 or 403. So a 404 has four distinct causes and they need different fixes:
  1. **Permissions.** The key's category or team scope doesn't cover the resource. This is the one that masquerades as absence: the story exists, the key just can't see it. Check the key's team scope before telling the user the story doesn't exist.
  2. **Wrong `host`.** A typo in the tenant subdomain, or a stray scheme or `/api/v1` baked into the `host` field (`host` is bare FQDN only). This usually 404s *every* call, including `/api/v1/stories` -- that pattern is the tell.
  3. **Genuinely absent id.** Re-run `query` rather than guessing adjacent ids.
  4. **For `list-agents` only: neither action path exists.** The CLI already tries the documented `/api/v1/actions` and then the legacy `/api/v1/agents`; if both 404 while `query` works, this is a very old or very new self-hosted build rather than anything you can fix from here. Fall back to `query --filter SEND_TO_STORY_ENABLED` plus the story's `entry_agent_ids` when you only need trigger capability, and report the endpoint gap.

  Distinguish these by whether a plain `query` returns anything: if the story list works but one id 404s, suspect permissions or a stale id; if only `list-agents` 404s, suspect cause 4; if everything 404s, suspect `host`.
- **422** -- a filter value the API rejected. Surface the body verbatim; Tines names the offending field.
- **429** -- rate limit. Tines enforces a per-token cap (observed around 3,600 requests/hour, plan-dependent) *and* an aggregate per-tenant cap above it. Back off and re-issue with a smaller `--limit`; do **not** loop-retry without backoff. Tines does return `X-RateLimit-Limit` / `-Remaining` / `-Reset` headers, but **this connector cannot read them** -- the proxy hands the CLI only a status code and a body, so there is no way to self-throttle before hitting the wall or to know exactly when the window resets. Treat 429 as a hard stop for the turn rather than something to pace against.
- **TLS handshake / certificate error** -- self-hosted tenant behind a private CA with **Skip TLS verification** off. Install-time configuration; surface and stop.
- **Empty result on a 200** -- a real, correct, empty result (the CLI prints `No query results (0 rows) for '<filter>'`). Not a failure -- don't retry. If the user expected rows, the usual causes in order: the tag's **case** doesn't match (`remediation` vs `Remediation`), `--tag` was given a substring rather than a whole tag, the stories are untagged, or the key's team scope doesn't cover them. Confirm the real tag spelling from a `tag_list` on an unfiltered `query` before retrying.

## Guardrails

- **Read-only.** Story execution is not reachable from this CLI. Neither Send-to-Story (`POST /api/v1/stories/:id/events`) nor the webhook trigger (`POST /webhook/<path>/<secret>`) is exposed, and the connector's API key needs no permission beyond reading stories. If the user asks to run a story, tell them this connector version doesn't execute and point at the Tines UI or a workflow skill -- do **not** improvise a write through another subcommand.
- **Webhook secrets never leave the connector.** The actions endpoint returns `options.secret` for every webhook action, and that secret *is* the full auth for that webhook -- `https://<host>/webhook/<path>/<secret>` needs nothing else. The CLI scrubs every `secret` key, at any depth, **before** the payload is rendered, cached for `view-result`, sampled, or upserted into the knowledge graph. Do not ask the operator to fetch a raw secret out of band to work around this, and do not reconstruct a webhook URL from a path plus a secret sourced from chat history.
- **Never construct a webhook URL the CLI didn't return.** No guessing a path from a story name.
- **No automated verdicts** -- no `CONFIRMED` / `LIKELY` scoring, no "this story should be run".
- **Render before pivoting** -- answer the question asked; don't pre-emptively chain queries across every story in the tenant.
- **Respect scope** -- "stories tagged X" -> `--tag X`, not "first 50 sorted by id".

## Connector

- **Base URL:** `https://<host>` (`host` is the bare Tines tenant FQDN; no scheme, no `/api/v1`).
- **Auth:** `Authorization: Bearer <api_key>`, injected server-side from the `api_key` secret. A Team- or Service-category key; no execution permission needed.
- **Category:** `querying`.
- **Required runtime env:** `CROGL_MCP_URL`, `CROGL_CLI_TOKEN` (supplied by the agent container).
- **Output:** grouped PSV for lists (with a result `ref`), pretty-printed JSON for `get-story`.
