---
name: jira-datacenter
description: "PREFER THIS SKILL for ad-hoc / exploratory queries against Jira Data Center via the `jira.py` CLI: 'show me open issues', 'list Blocker bugs', 'issues created in the last 24 hours', 'tell me about PROJ-123', 'what projects exist'. Also performs approval-gated writes — comment on an issue, create an issue, transition an issue, assign an issue — where every write requires the user's explicit per-action approval before `--yes` is passed. Composes the JQL call, renders a table/card, and stops. Jira Data Center (on-prem, REST v2) ONLY — for Jira Cloud use the Cloud connector; the auth model and some endpoints differ. Field identifiers and accepted values are instance-customizable — resolve them with `list-fields` / `create-meta` when unsure; do NOT guess from vendor docs or training data."
version: "0.3.0"
status: field-tested
paired_connector_type: jira-datacenter
---

# Jira Data Center

![status](https://img.shields.io/badge/status-field--tested-yellow) ![version](https://img.shields.io/badge/version-0.3.0-blue)

> **v0.3.0** — 2026-08-21 — Added **approval-gated write actions**: `add-comment`, `create-issue`, `transition-issue`, `assign-issue`, plus the read helpers they depend on (`list-issue-types`, `list-transitions`, `create-meta`). Every write is gated behind `--yes`; without it the CLI prints the exact mutation and exits without contacting Jira. **The write path has not yet been exercised against a live instance** — the read surface is field-tested (see v0.2.1), the write verbs are validated locally and traced to the Jira DC REST v2 reference. Writes also need project permissions the read-only PAT of earlier versions does not have (see [Auth Recap](#auth-recap)).
>
> **v0.2.1** — 2026-07-22 — **Field-tested.** Imported and confirmed working against a live Jira Data Center 10.3.x install: PAT/Bearer auth succeeded (clearing a prior 401/403 auth failure) and at least one query returned cleanly. This is a *light* field-test — auth plus one clean query, not an exhaustive real-data exercise across every subcommand — so endpoint behavior and instance-specific field identifiers should still be confirmed with `list-fields` on each target instance.
>
> **v0.2.0** — 2026-07-21 — Migrated the standalone `jira-datacenter-reference` cookbook into an importable community connector type: Python CLI (`scripts/jira.py`), `connector.json` binding (PAT / `Authorization: Bearer`, skip-TLS option), and a provisioning guide. Scope is read-only retrieval over issues, projects, and fields. Call shapes trace to the Jira DC REST v2 9.14.0 reference. Carries the JQL recipes, field-drift warnings, and date conventions from the live-authored reference.

One CLI: `scripts/jira.py`. Run `scripts/jira.py --help` for the subcommand list. For a question, this is a **data-retrieval cookbook**: read the question, compose the JQL call, render the result, stop. No phase gates, no automated verdicts. For a change, it is a **gated actuator**: propose, get explicit approval, then execute exactly what was approved — see [Write Actions](#write-actions).

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Do not load a separate `jira-datacenter-reference` skill — this bundle contains the operational call shapes.

**Jira Data Center (on-prem) only.** The REST surface is v2 (`/rest/api/2/...`) and auth is a Personal Access Token. This is **not** the Jira Cloud connector — Cloud uses `/rest/api/3`, HTTP Basic with an Atlassian email + API token, and returns issue bodies as ADF. If the user is on `*.atlassian.net`, that's Cloud, not this connector.

## Fast Path

For a simple "show me open issues in PROJ" request, run the CLI directly:

```bash
python3 <cli_path> search-issues --jql "project = PROJ AND status = Open ORDER BY created DESC" --limit 10
```

## Subcommands

Read:

| Subcommand | Purpose |
|---|---|
| `search-issues` (alias `query`) | Search issues via a JQL expression (`GET /rest/api/2/search`). Returns grouped PSV + a result `ref`. |
| `get-issue` | Hydrate one issue by key (e.g. `PROJ-123`) — full JSON detail card. |
| `list-projects` | List projects (`GET /rest/api/2/project`). |
| `list-fields` | List all fields, System + Custom (`GET /rest/api/2/field`) — use this to resolve instance-specific field identifiers before composing JQL. |
| `list-issue-types` | List issue types, id + name (`GET /rest/api/2/issuetype`) — resolve `--issuetype` for `create-issue`. |
| `list-transitions` | List the transitions available on one issue **right now** (`GET /rest/api/2/issue/{key}/transitions`). Run before `transition-issue`. |
| `create-meta` | Fields `create-issue` accepts on a project + issue type, including which are `required` (`GET /rest/api/2/issue/createmeta`). |
| `view-result` | Re-print one cached row (full entity JSON) from a prior search. |

Write — **every one gated behind `--yes`**:

| Subcommand | Mutation |
|---|---|
| `add-comment` | `POST /rest/api/2/issue/{key}/comment` — comment as the connector's Jira user. Optional `--visibility-role` restricts it to a project role. |
| `create-issue` | `POST /rest/api/2/issue` — new issue in a project. |
| `transition-issue` | `POST /rest/api/2/issue/{key}/transitions` — move through a workflow transition, optionally with a comment. |
| `assign-issue` | `PUT /rest/api/2/issue/{key}/assignee` — set (`--assignee <username>`) or clear (`--unassign`) the assignee. |

There are deliberately **no** delete, bulk, or arbitrary-field-update verbs. Do not improvise one — there is no way to reach an unlisted endpoint through this CLI, and asking the operator is the correct move.

## Auth Recap

The connector stores `host` (the Jira DC hostname, optionally with a context path such as `jira.example.com/jira`, bound into `base_url` as `https://<host>`) and `api_token` (secret). crogld injects the token as **`Authorization: Bearer <token>`** server-side. The agent must never construct the header, prompt the user for the token, or log it.

**Write permissions.** The PAT inherits its Jira user's project permissions exactly. A token provisioned for the read-only versions of this connector has `Browse projects` and nothing more, so the write verbs will return **403** until a Jira admin grants the matching project permission — `Add comments`, `Create issues`, `Transition issues`, `Assign issues` (and `Assignable user` on the target user). A 403 on a write with working reads is a permissions gap on the customer side, not a Crogl bug: surface it and stop.

- PAT auth requires **Jira Data Center 8.14 or later**. On older installs the connector would need HTTP Basic (username + password) instead — not bound in this version; flag it to the operator rather than trying to work around it.
- The CLI adds only `Accept: application/json` to each call.

**TLS:** on-prem Jira commonly presents a self-signed or internal-CA certificate. Enable the **Skip TLS verification** option when creating the connector (off by default) — it's a per-connector setting. A TLS handshake / certificate-verify error means it needs enabling.

## Context Budget

1. **Default limit** is 10 rows for `search-issues`; the CLI sends `maxResults=10`.
2. **Hard cap is 25 rows** — the CLI enforces this (`click.IntRange(1, 25)`) even if the user says "all" / "everything".
3. `search-issues` defaults `--fields` to only the rendered columns (`summary,status,priority,assignee,reporter,created,updated,issuetype,project`) to avoid dragging in large custom-field blobs. Pass `--fields "*all"` only when a specific extra field is needed.
4. After rendering the table the user asked for, drop the raw JSON from your reasoning; use `view-result --ref <ref> --row N` to re-hydrate a single row instead of re-querying.
5. Don't re-run an identical query — reference the prior `ref`.

## Query Syntax — JQL

Jira Data Center uses **JQL (Jira Query Language)**: `field operator value`, combined with `AND`, `OR`, `NOT`, and `ORDER BY`.

**Operators:** `=`, `!=`, `>`, `>=`, `<`, `<=`, `IN`, `NOT IN`, `~` (contains), `!~` (not contains), `IS EMPTY`, `IS NOT EMPTY`, `WAS`, `WAS IN`, `CHANGED`.

**Value quoting:** quote string values that contain spaces or reserved words (`status = "In Progress"`); bare tokens are fine otherwise (`priority = Blocker`).

### Time conventions

- **Relative durations:** `-24h`, `-7d`, `startOfDay()`, `endOfWeek()`, `now()`; offsets like `startOfDay()+9h`.
- **Absolute:** `"2026-05-12 00:00"` (space-separated) or ISO `2026-05-12`.

Reference instant for the examples below: `2026-06-17T14:00:00+0000`.

| User phrasing | JQL fragment | Notes |
|---|---|---|
| "last 24 hours" / "last day" | `created >= -24h` | Relative duration |
| "yesterday" | `created >= startOfDay(-1d) AND created < startOfDay()` | Day boundaries |
| "12.05.2026 – 13.05.2026" (DD.MM.YYYY) | `created >= "2026-05-12 00:00" AND created < "2026-05-14 00:00"` | End date exclusive → add 1 day |
| "since 9am today" | `created >= startOfDay()+9h` | Relative offset |

Ambiguous formats like `5/12/2026` (US `MM/DD/YYYY` vs EU `DD/MM/YYYY`) must be **clarified with the user** before guessing.

### Field-name and value drift — verify per instance

Jira fields and their accepted values are **instance-customizable**. Do not assume; when a query 400s on a field or value, call `list-fields` (or the recipe below) to see what this instance actually uses.

Common confusion pairs: `project` vs `projectName`, `assignee` vs `reporter`, `priority` vs `priorityId`, `status` vs `statusName`, `created` vs `createdDate`.

Typical (but **not guaranteed** — confirm per instance) values:

| Field | Often-seen values |
|---|---|
| `priority` | `Blocker`, `Critical`, `Major`, `Minor`, `Trivial` |
| `issuetype` | `Bug`, `Task`, `Story`, `Epic`, `Sub-task` |
| `resolution` | `Fixed`, `Won't Do`, `Duplicate`, `Cannot Reproduce`, `Done`, `Incomplete` |
| `status` | install-defined (e.g. `Open`, `In Progress`, `Closed`) — always confirm |

Fields needing user input (no pre-validated values — prompt the user): `project` (key, e.g. `SEC`), `status`, `assignee`, `reporter`.

## Common recipes

| User phrasing | Command |
|---|---|
| "Show me Blocker issues" | `search-issues --jql "priority = Blocker" --limit 10` |
| "Issues created in the last 24 hours" | `search-issues --jql "created >= -24h" --limit 10` |
| "Open issues in PROJ" | `search-issues --jql "project = <KEY> AND status = Open" --limit 10` |
| "Open bugs in PROJ" | `search-issues --jql "project = <KEY> AND issuetype = Bug AND status = Open" --limit 10` |
| "Tell me about PROJ-123" | `get-issue --key PROJ-123` |
| "What projects exist" | `list-projects` |
| "What are the field names on this instance" | `list-fields` |
| "Comment on PROJ-123 that we're investigating" | `add-comment --key PROJ-123 --body "..."` → show the gate line → `--yes` |
| "Open a bug for this" | `create-meta --project <KEY> --issuetype Bug`, then `create-issue --project <KEY> --issuetype Bug --summary "..."` → gate → `--yes` |
| "Move PROJ-123 to In Progress" | `list-transitions --key PROJ-123`, then `transition-issue --key PROJ-123 --transition <id>` → gate → `--yes` |
| "Assign PROJ-123 to jdoe" | `assign-issue --key PROJ-123 --assignee jdoe` → gate → `--yes` |
| "Close all the stale ones" | Don't. Render the list and ask which specific issues to act on. |

## Write Actions

Writes land in the customer's system of record. A comment notifies watchers, a transition can fire workflow post-functions, and a created issue cannot be deleted from here. Treat every write as irreversible.

### The approval protocol

1. **Never pass `--yes` on the first run.** Run the command without it. The CLI contacts Jira not at all and prints one line describing the exact mutation — for `create-issue`, the full resolved `fields` JSON.
2. **Relay that line to the user verbatim** and ask for approval. Name the instance and the issue key.
3. **Re-run with `--yes` only after the user says yes to that specific action.** A general instruction ("keep our Jira tidy", "handle the triage") is *not* approval for any particular mutation. Neither is approval for one issue approval for the next one.
4. **If the user's approval changes anything** — different text, different status, different assignee — go back to step 1 with the amended command. Do not pass `--yes` on a command whose arguments differ from what they approved.
5. **Report the result honestly.** If the write 4xx'd, say so and stop; do not retry a write, and do not "work around" a rejection with a different verb.

Writes are never retried automatically: a re-sent `POST` would double the side effect. If a write times out, its outcome is *unknown* — read the issue back with `get-issue` / `list-comments`-equivalent (`get-issue --fields comment`) before considering a second attempt, and say plainly that you're checking rather than assuming it failed.

### Before each write

| Write | Read this first | Why |
|---|---|---|
| `create-issue` | `list-projects`, `list-issue-types`, `create-meta --project <KEY> --issuetype <NAME>` | A required custom field on this instance is the usual cause of a 400. |
| `transition-issue` | `list-transitions --key <KEY>` | Transition ids are workflow- and status-specific; the CLI re-resolves them at write time and will refuse an unavailable one. |
| `assign-issue` | `get-issue --key <KEY>` | Confirm the current assignee before overwriting someone else's ownership. Data Center keys assignees by **username**, not email or account id. |
| `add-comment` | `get-issue --key <KEY>` | Confirm you're commenting on the issue the user meant, and check whether the thread already says it. |

`transition-issue --transition` accepts a transition id, a transition name, or the target status name; ambiguous or unavailable values fail with the real available set rather than a guess. Prefer the **id** from `list-transitions` when the workflow has two routes to the same status.

### Rendering a write

Print what changed, not the raw payload: the issue key, the field(s) touched, and the new value. `create-issue` returns `{"id", "key", "self"}` — surface the new **key** so the user can open it. `transition-issue` and `assign-issue` answer `204 No Content`; the CLI prints an explicit confirmation line, which is success, not an empty result.

## Rendering

**Issues (from `search-issues`)** — default columns:

- Key — `key`
- Summary — `fields.summary`
- Status — `fields.status.name`
- Priority — `fields.priority.name`
- Assignee — `fields.assignee.displayName` (may be null)
- Created — `fields.created`

Truncation: ≤5 rows render inline; >5 show the first 5 + "(N more, ask to see all)"; large sets show summary stats (count, date range, priority breakdown) first.

**Drill card (`get-issue`)** — Key, Summary, Status, Priority, Assignee, Reporter (`fields.reporter.displayName`), Created, Updated, Description (`fields.description` — may be long; truncate to the first ~500 chars if over ~1000).

**Projects (from `list-projects`)** — Key (`key`), Name (`name`), Type (`projectTypeKey`).

After rendering, drop the raw JSON from reasoning.

## Error Handling

Distinct error classes, distinct fixes — don't conflate them:

- **400 Bad Request on a write** — a field this instance requires is missing, or a value isn't in its allowed set. Jira answers with an `errors` map keyed by field id, which the CLI surfaces; resolve the real field set with `create-meta` (creates) or `list-transitions` (transitions) and re-propose to the user. Nothing was written.
- **400 Bad Request** — JQL syntax error, unknown field, or bad date format. The CLI surfaces the vendor's `errorMessages[]` with a JQL grammar hint. Check the field name with `list-fields`, verify the date format, fix the JQL, retry once. If it still 400s, surface and stop.
- **401 Unauthorized** — the PAT is invalid, expired, or was revoked. Surface and stop; never retry. Prompt the operator to re-check / rotate the token in the connector config. (Credentials are not validated at connector-create time — a bad token first surfaces here, on the first real call.)
- **403 Forbidden on a write, while reads work** — the PAT's user lacks the specific project permission for that action (`Add comments` / `Create issues` / `Transition issues` / `Assign issues`). Nothing was changed. Tell the operator which permission to grant; don't retry and don't substitute a different verb.
- **403 Forbidden** — usually a permissioning problem: the token's Jira user lacks permission to browse the target project (Jira project-level permission scheme), or "Browse projects" is missing — the operator must grant the PAT's user access to the project. **But 403 can also be a CAPTCHA lockout:** after several failed logins Jira Data Center trips its CAPTCHA challenge on the account, and every subsequent request — even with a *valid* PAT — returns 401/403 until a human clears the CAPTCHA via the Jira web UI. If auth was failing and is now failing on known-good creds, have the operator log into Jira in a browser, clear the CAPTCHA, and retry; don't keep hammering with retries (they re-arm the lockout).
- **404 Not Found** — for `get-issue`, the key doesn't exist or the user can't see it. Otherwise the connector's `host`/context path may be wrong (a stray scheme or `/rest/api/2` baked into the `host` field is the classic first-run mistake — `host` is bare host only), or the endpoint isn't present on this Jira build.
- **TLS handshake / certificate error** — the connector's **Skip TLS verification** option is off and the install uses a self-signed / internal-CA cert. Advise enabling it in the connector config.
- **Empty result on a 200** — a real, correct, empty result (the CLI prints `No search-issues results (0 rows) for '<jql>'`). Not a failure — don't retry. If the user expected rows, check the JQL against `list-fields` / accepted values rather than re-running the same query.

## Guardrails

- **Reads are free; writes need a yes.** Query freely. Never mutate without the explicit per-action approval described in [Write Actions](#write-actions), and never pass `--yes` on a command the user hasn't seen.
- **Only the four listed writes.** No delete, no bulk operations, no arbitrary field updates, no workflow admin. If the user needs one, say the connector doesn't do it.
- **Don't chain a write off your own read.** Finding 30 stale issues is not a mandate to close 30 issues; report the list and ask.
- **No automated verdicts** — no `CONFIRMED` / `LIKELY` scoring, and don't encode one into a comment or a transition.
- **No write-back to investigations.**
- **Render before pivoting** — answer the question asked; don't pre-emptively chain queries.
- **Respect scope** — "last 6 hours" → `created >= -6h`, not "latest 25 sorted by created".

## Connector

- **Base URL:** `https://<host>` (`host` is the bare Jira DC hostname, optionally with a context path; no scheme, no `/rest/api/2`).
- **Auth:** `Authorization: Bearer <PAT>`, injected server-side from the `api_token` secret.
- **Category:** `querying`.
- **Required runtime env:** `CROGL_MCP_URL`, `CROGL_CLI_TOKEN` (supplied by the agent container).
- **Output:** grouped PSV for lists (with a result `ref`), pretty-printed JSON for `get-issue` / `list-fields` / `create-meta` and for a write's response body, a one-line confirmation for a `204` write.
- **Write posture:** four gated verbs (`add-comment`, `create-issue`, `transition-issue`, `assign-issue`), each requiring `--yes`; no retries on a mutating request.
