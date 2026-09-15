---
name: servicenow-chatops
description: "Create and track a ServiceNow ChatOps verification request via the `servicenow_chatops.py` CLI: send a question to a real employee (assigned to a record in a customer-configured table their ServiceNow ChatOps/Now Actions flow already delivers to Slack/Microsoft Teams), then poll for their reply. Performs approval-gated writes (`send-verification`, `add-comment`, `close`, each requiring the user's explicit approval before `--yes`) plus read-only `check-reply`/`query`. Does NOT talk to Slack/Teams directly, does NOT support a live, single-turn wait for the reply, and is scoped to ONE table per connector instance -- see Guardrails and the platform-limitation note below before assuming this can do more than that."
version: "0.1.0"
status: draft
paired_connector_type: servicenow-chatops
---

# ServiceNow ChatOps

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-02 — Initial docs-only build. Every call shape traces to ServiceNow's official Table API and OAuth documentation, or to the shipped `crogl-servicenow` built-in skill's already-working implementation (see Auth Recap and Error Handling for which). **Nothing in this connector has been exercised against a live ServiceNow instance.** No credentials were available at build time; every claim not independently corroborated by the built-in is flagged inline as unverified. Do not promote past `draft` until a live `send-verification` has actually created a record and a live `check-reply` has actually returned a reply.

One CLI: `scripts/servicenow_chatops.py`. Run `scripts/servicenow_chatops.py --help` for the subcommand list.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name.

## What this connector is (and is not)

This connector creates and reads records in **one ServiceNow table, fixed per connector instance** — whatever table the customer's own ServiceNow ChatOps / Now Actions notification flow is already configured to watch and deliver to Slack or Microsoft Teams. It does **not** talk to Slack/Teams itself, and does **not** configure that delivery flow — that is the customer's existing ServiceNow setup, outside this connector's control. If that flow isn't configured to watch this table, `send-verification` still creates a record, but nothing reaches the target user in chat.

This connector is **not** a live, single-turn "ask and wait for the answer" tool. `send-verification` creates a record and ends; a **separate, later** call to `check-reply` is required to see whether the target user has responded. There is no automatic mechanism that resumes an investigation when the reply arrives — a human or a scheduled re-check has to trigger `check-reply`. Do not describe this to a user as "checking in live" or imply the agent will pause and wait; it will not.

If the request is instead a general "query any ServiceNow table" need — incidents, changes, CMDB, arbitrary field lookups — that is a **different, already-existing capability**: the `crogl-servicenow` built-in skill (`query`, `get-record`, `add-comment`, KG catalog walk). This connector deliberately does not duplicate that read surface fully; it exists to add the one thing that built-in lacks — creating a new record (`send-verification`).

## Fast Path

```bash
python3 <cli_path> send-verification --user john.smith --question "Are you traveling to Budapest this week, and was it authorized through HR?"
# shows the gate line -- relay it, get explicit approval, then:
python3 <cli_path> send-verification --user john.smith --question "..." --yes
python3 <cli_path> check-reply --sys-id <sys_id>
```

## Subcommands

Read:

| Subcommand | Purpose |
|---|---|
| `check-reply` | Fetch one verification-request record by `--sys-id` — current assignee, state, description, and the full `comments` journal field. |
| `query` | List/search records on the target table, with an optional raw ServiceNow encoded query (`sysparm_query`). Grouped PSV + a result `ref`. |
| `view-result` | Re-print one cached row (full record JSON) from a prior `query`. |

Write — **all three gated behind `--yes`**:

| Subcommand | Mutation |
|---|---|
| `send-verification` | `POST` a new record in the configured target table, `assigned_to` the target user, with the question in `description`. |
| `add-comment` | `PATCH` the `comments` journal field without changing state. |
| `close` | `PATCH` the record's `state` (and optionally add a closing comment) once resolved. |

## Auth Recap

The connector stores `host` (ServiceNow instance hostname), `target_table` (the fixed table this instance targets), `client_id`, and `client_secret`. crogld mints an OAuth2 Client Credentials token server-side (`POST https://<host>/oauth_token.do`, form-encoded `grant_type=client_credentials&client_id=...&client_secret=...`) and injects it as **`Authorization: Bearer <token>`**. The agent must never construct this header, prompt the user for the token, or log it.

- **Deliberately not HTTP Basic**, unlike the built-in `crogl-servicenow` skill's username/password auth. ServiceNow's own documentation calls Basic Auth "fine for development, not recommended for production"; since this connector performs writes tied to real employees, OAuth2 Client Credentials (via a dedicated integration user on the OAuth Application Registry, no password to rotate) is the more defensible choice for a write-capable connector. Requires ServiceNow **Washington DC release or later** for inbound Client Credentials support — confirmed via ServiceNow's own community documentation on this grant type; not independently re-verified against a live instance.
- **`target_table` is fixed at connector-configuration time**, baked into the base URL (`https://<host>/api/now/table/<target_table>`) — not a per-call argument. This is deliberate: there is no verified way for this CLI to read a non-secret `connector.json` field at runtime that isn't part of the HTTP binding, and even if there were, letting the agent pick an arbitrary table per call would defeat the point of a customer-approved ChatOps delivery table. One connector instance targets exactly one table; a customer needing two tables needs two connector instances.
- **The target-user field is hardcoded to `assigned_to`**, not configurable. Standard on ServiceNow's Task-derived tables (`incident`, `sc_task`, `interaction`); not guaranteed on a fully custom table — confirm the target table has this field before provisioning.

## Context Budget

1. **Default limit** is 10 rows for `query`; hard cap is 25 (`click.IntRange(1, 25)`), same as jira-datacenter and other connectors in this repo.
2. `query` defaults `--fields` to `sys_id,number,short_description,assigned_to,state,sys_created_on` — pass `--fields` explicitly to widen.
3. After rendering, drop raw JSON from reasoning; use `view-result --ref <ref> --row N` to re-hydrate a single row instead of re-querying.

## Query Syntax — ServiceNow Encoded Query (sysparm_query)

`field=value^field2LIKEterm` — `^` is AND, `^OR` is OR. Example: `active=true^assigned_to.email=john.smith@example.com`.

Field names and `state`/`priority` values are **instance-customizable** — do not assume; confirm with the customer's ServiceNow admin, or run `query` with no `--query` filter first to see what values actually appear, before filtering on a guessed value. This mirrors jira-datacenter's field-drift caveat exactly — same underlying problem (an on-prem/tenant-customized system), different vendor.

## Write Actions

### The approval protocol

Identical to jira-datacenter's, and for the same reason — writes here are worse to get wrong, not better: `send-verification` notifies a real employee in chat (if the customer's ChatOps flow is configured), and a mistaken send cannot be recalled from this connector.

1. **Never pass `--yes` on the first run.** The CLI prints the exact mutation and exits without contacting ServiceNow.
2. **Relay that line to the user verbatim** and get explicit approval — name the target user and the question, not just "send a verification."
3. **Re-run with `--yes` only after that specific approval.** A general instruction ("check in with anyone whose creds look off") is not approval for one specific person.
4. **If the approved details change, go back to step 1** with the amended command.
5. **Report the result honestly.** If the write 4xx'd, say so and stop.

Writes are never retried automatically — a re-sent `POST` would notify the target user twice, and a re-sent `PATCH` risks clobbering a state change made in between.

### Before each write

| Write | Read this first | Why |
|---|---|---|
| `send-verification` | Nothing required, but confirm the target user's username/email is correct — a typo sends the request (and any real chat notification) to the wrong person, or to no one. |
| `close` | `check-reply --sys-id <sys_id>` | Confirm the reply actually resolves the question before closing, and get the current state value if `--state` is a guess. |

## Rendering

**`query`** — default columns: sys_id, number, short_description, assigned_to (display value), state, sys_created_on. Truncation matches jira-datacenter: ≤5 rows inline, >5 shows first 5 + "(N more, ask to see all)".

**`check-reply`** — full flattened JSON: number, short_description, description (the original question), assigned_to, state, comments (full journal — see the caveat below), sys_updated_on.

After rendering, drop raw JSON from reasoning.

## Error Handling

Distinct error classes, distinct fixes:

- **400/401 on token mint** — `client_id`/`client_secret` mismatch, or the OAuth Application Registry entry isn't configured for Client Credentials grant. Isolate with a direct token-mint request (curl) against `https://<host>/oauth_token.do` outside Crogl entirely before touching anything else, per this repo's `/build-connector` triage guidance.
- **403 on `send-verification`/`add-comment`/`close`, while `query`/`check-reply` work** — the OAuth integration user lacks the `write` role on the target table (ServiceNow's own docs: production Client Credentials setups should use a dedicated integration user with only the roles the integration needs). Nothing was written; tell the operator which role to grant, don't retry.
- **400 on `send-verification`** — a required field on the target table wasn't supplied. Resolve with `--field KEY=VALUE`; there is no `create-meta`-equivalent in this connector to discover required fields automatically (unlike jira-datacenter) — ask the customer's ServiceNow admin, or use the `crogl-servicenow` built-in's `get-record`/schema tools if the customer has that skill enabled.
- **404** — wrong `sys_id`, or the connector's `host`/`target_table` binding is misconfigured (a stray scheme or `/api/now/table` baked into the `host` field is the classic first-run mistake — `host` is bare host only).
- **Empty result on a `query` 200** — a real, correct, empty result (`No query results (0 rows)`), not a failure. Don't retry blindly; check the query against the target table's actual field/value set first.
- **`send-verification` succeeded but the target user never responds** — this connector cannot distinguish "the customer's ChatOps flow isn't actually configured to watch this table" from "the target user just hasn't replied yet." If `check-reply` shows no new `comments` after a reasonable wait, say so plainly and suggest the operator confirm the ChatOps flow is live for this table — do not assume it's simply pending.

## Guardrails

- **Reads are free; writes need a yes.** `query`/`check-reply` freely; never `send-verification`/`add-comment`/`close` without the explicit per-action approval above.
- **This is not a live wait.** Never tell a user the agent is "checking in now and will wait for the answer" — it creates a record and the turn ends. A separate `check-reply` call, later, is required.
- **Only the three listed writes.** No delete, no bulk sends, no arbitrary-field PATCH beyond what `--field`/`close --state` expose.
- **One table per connector instance, not agent-selectable.** Never attempt to target a different table than the connector is configured for.
- **No automated verdicts** on whether a target user's reply "confirms" or "denies" anything — render their actual reply text and let the analyst judge it.
- **Never fabricate a target user's reply.** If `check-reply` shows no new comment, say so — do not infer or guess what they "probably" would have said.

## Connector

- **Base URL:** `https://<host>/api/now/table/<target_table>` (`host` bare hostname; `target_table` fixed per instance).
- **Auth:** OAuth2 Client Credentials, `Authorization: Bearer <token>`, injected server-side from `client_id`/`client_secret`.
- **Category:** `ticketing`.
- **Required runtime env:** `CROGL_MCP_URL`, `CROGL_CLI_TOKEN`.
- **Output:** grouped PSV for `query` (with a result `ref`), pretty-printed JSON for `check-reply`/`view-result`, a one-line confirmation for `send-verification`/`add-comment`/`close`.
- **Write posture:** three gated verbs (`send-verification`, `add-comment`, `close`), each requiring `--yes`; no retries on a mutating request.
- **Related:** the `crogl-servicenow` built-in skill covers general any-table querying and comment-adding; this connector is deliberately narrower and adds only record creation.
