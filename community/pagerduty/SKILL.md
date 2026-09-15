---
name: pagerduty
description: "PREFER THIS SKILL for anything involving PagerDuty via the `pagerduty.py` CLI: escalating a correlated/critical Crogl finding to PagerDuty so on-call gets paged, checking who is on call, listing/searching existing incidents, acknowledging/resolving/reassigning one, or commenting on one. Core use case is unattended escalation for a customer without 24/7 SOC coverage: when Crogl correlates alerts into a critical-priority finding, this pages the right on-call via `create-incident`, with NO approval gate -- see Guardrails. Covers PagerDuty's REST API v2 incident lifecycle ONLY (create/list/get/update an incident, add a note, plus the services/escalation-policies/priorities/on-calls lookups to target one correctly) -- NOT Events API v2 (routing-key event ingestion), a separate unbuilt connector. The customer's SIEM/case-management connector stays the system of record; this is purely the paging channel."
version: "0.1.0"
status: draft
paired_connector_type: pagerduty
---

# PagerDuty

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-14 — Initial community connector type. Scope and API shape verified against PagerDuty's official OpenAPI spec (`PagerDuty/api-schema` on GitHub, `reference/REST/openapiv3.json`) and `support.pagerduty.com` provisioning docs, not vendor prose alone. **Not yet live-tested against a real PagerDuty account** — see the changelog note this gets updated with once that happens, and treat every claim below as best-effort/verify-before-relying-on-it until this connector reaches `field-tested`.

One CLI: `scripts/pagerduty.py`. Run `scripts/pagerduty.py --help` for the subcommand list. For a read, this is a **data-retrieval cookbook**: read the question, compose the call, render the result, stop. For `create-incident`, it is an **unattended actuator**: this connector's whole purpose is escalating a Crogl-correlated finding to PagerDuty without waiting on a human — see [Guardrails](#guardrails) before assuming that's a mistake. For `update-incident`/`add-comment`, it is a **gated actuator**: propose, get explicit approval, then execute exactly what was approved.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Do not import a separate PagerDuty reference skill — this bundle contains the operational call shapes.

## Scope

This connector covers PagerDuty's **REST API v2 incident lifecycle** only:

- Create/escalate an incident (`POST /incidents`), list/search incidents (`GET /incidents`), get one incident (`GET /incidents/{id}`), update one — acknowledge/resolve/reopen/reprioritize/reassign (`PUT /incidents/{id}`) — and add a note (`POST /incidents/{id}/notes`).
- The read-only lookups needed to target those correctly: services, escalation policies, priorities, on-calls.

**Deliberately out of scope** (see the changelog above and this repo's `CONTRIBUTING.md` on scoping a broad product to one coherent sub-surface):

- **PagerDuty's Events API v2** (`POST https://events.pagerduty.com/v2/enqueue`) — a structurally different surface: auth is a per-service routing/integration key (not this connector's REST API key + user email), and it's designed for raw monitoring-tool event ingestion. PagerDuty's own API spec explicitly recommends the REST API's synchronous Incidents API instead for a caller that is itself "a system of record" making an analyzed decision to escalate — which is exactly what Crogl is doing here (correlating SIEM alerts, not forwarding raw monitor pings). If a future need requires triggering incidents from a routing key directly, that's a separate `pagerduty-events` connector type, not an extension of this one.
- Schedules (create/edit an on-call rotation), users/teams CRUD, maintenance windows, webhooks/extensions, and every other REST API v2 resource outside the incident lifecycle above.

## Auth Recap

The connector stores two credential fields, both marked secret in the connector config even though only one actually is:

- **`api_key`** (genuinely secret) — a PagerDuty REST API v2 key. Injected server-side as **`Authorization: Token token=<key>`**. Must be a **full-access** key, not the "Read-only API Key" option PagerDuty's key-creation form offers — a read-only key will 403 every write verb here while reads keep working (see [Error Handling](#error-handling)).
- **`from_email`** (not actually secret) — the email of a real, active PagerDuty user on the account. Injected server-side as **`From: <email>`** on every call. PagerDuty only requires `From` on writes (create/update an incident, add a note) and ignores it on reads, but `connector.json`'s header-binding schema requires every bound header to carry a secret field, so this is marked `secret: true` purely to satisfy that — the tradeoff (traced directly against `croglng`'s binding validator, not assumed) is this value can't be viewed or edited after the connector is created, only replaced, even though it isn't sensitive data. If the acting user's email needs to change, replace the field rather than looking for an edit option.

The agent must never construct either header, prompt the user for the API key, or log it.

## Fast Path

For "page on-call for this", once you know which service:

```bash
python3 <cli_path> create-incident --service <service-id> --title "..." --urgency high --incident-key <stable-key>
```

For "who's on call right now":

```bash
python3 <cli_path> list-oncalls --escalation-policy-id <id> --earliest
```

## Subcommands

Read:

| Subcommand | Purpose |
|---|---|
| `list-services` | List services (`GET /services`), optionally filtered by `--query`/`--name`/`--team-id`. Resolve the `--service` id for `create-incident`. |
| `list-escalation-policies` | List escalation policies (`GET /escalation_policies`). Resolve the `--escalation-policy` override id. |
| `list-priorities` | List priorities, most to least severe (`GET /priorities`). Resolve the `--priority` id. Empty if the account hasn't enabled the Priorities feature — unconfirmed live, see the changelog. |
| `list-oncalls` | List on-call entries (`GET /oncalls`) — who's on call, for which escalation policy, now or over a window. `--earliest` narrows to "who's on call right now" per (policy, level, user). |
| `list-incidents` (alias `query`) | Search incidents (`GET /incidents`) by status/urgency/service/key/date range. Returns grouped PSV + a result `ref`. |
| `get` | Hydrate one incident by id — full JSON detail card. |
| `view-result` | Re-print one cached row (full entity JSON) from a prior `list-*`/`list-incidents`/`query` call. |

Write:

| Subcommand | Mutation | Gate |
|---|---|---|
| `create-incident` | `POST /incidents` — create/escalate an incident against a service. | **None — fires immediately.** See [Guardrails](#guardrails). |
| `update-incident` | `PUT /incidents/{id}` — acknowledge/resolve/reopen (`--status`), reprioritize (`--priority`), or reassign (`--escalation-policy` or `--assignee`, not both) an **existing** incident. | `--yes` |
| `add-comment` | `POST /incidents/{id}/notes` — note on an incident, attributed to the `from_email` user. | `--yes` |

There are deliberately **no** delete, bulk, schedule, or user/team-management verbs. Do not improvise one — there is no way to reach an unlisted endpoint through this CLI, and asking the operator is the correct move.

## Context Budget

1. **Default limit** is 25 rows for list commands (`list-services`, `list-escalation-policies`, `list-oncalls`, `list-incidents`/`query`); `list-priorities` defaults to the hard cap since accounts rarely define more than a handful.
2. **Hard cap is 100 rows** (`click.IntRange(1, 100)`) — matches PagerDuty's own documented per-page maximum for `GET /incidents`/`GET /services`/etc., so a single request always satisfies it. There is no multi-page loop; if a filtered call still returns exactly 100 rows, narrow the filters (status/urgency/service/date range) rather than assuming that's the complete result.
3. After rendering the table the user asked for, drop the raw JSON from your reasoning; use `view-result --ref <ref> --row N` to re-hydrate a single row instead of re-querying.
4. Don't re-run an identical query — reference the prior `ref`.

## Filter/Lookup Reference

PagerDuty's incident search is flat named REST params, not a query language — pass one filter flag per dimension rather than composing a query string.

| Flag (on `list-incidents`/`query`) | PagerDuty param | Notes |
|---|---|---|
| `--status` (repeatable) | `statuses[]` | `triggered`, `acknowledged`, `resolved`. Omit for all statuses. |
| `--urgency` (repeatable) | `urgencies[]` | `high`, `low`. |
| `--service-id` (repeatable) | `service_ids[]` | Get ids from `list-services`. |
| `--incident-key` | `incident_key` | Exact de-dup key match — use to check whether a `create-incident` call already landed. |
| `--since` / `--until` | `since` / `until` | ISO 8601. Max range is 6 months; default is the last month if both are omitted. |

| Flag (on `list-services`) | PagerDuty param | Notes |
|---|---|---|
| `--query` | `query` | Substring match against service name. |
| `--name` | `name` | Exact name match. |
| `--team-id` (repeatable) | `team_ids[]` | Requires the account to have the `teams` ability. |

`list-escalation-policies --query`/`--user-id`/`--team-id` and `list-oncalls --escalation-policy-id`/`--user-id`/`--schedule-id`/`--since`/`--until`/`--earliest` follow the same flat-param pattern — run `--help` on either for the full flag list rather than guessing an unlisted one.

## Resources

| Resource | Endpoint | KG-walkable? |
|---|---|---|
| Incidents | `GET /incidents`, `GET /incidents/{id}` | Yes — the only walkable resource (`refresh`/`ls`/`grep`/`upload-samples`). |
| Services | `GET /services`, `GET /services/{id}` | No — small, bounded lookup, always fetched fresh. |
| Escalation policies | `GET /escalation_policies`, `GET /escalation_policies/{id}` | No — same as Services. |
| Priorities | `GET /priorities` | No — same as Services; also may be empty if the account hasn't enabled Priorities. |
| On-calls | `GET /oncalls` | No — inherently time-windowed, not a stable catalog entry. |

## Uploading samples

`upload-samples` enforces a read-before-write check: the rows must come from a `list-incidents`/`query` call the agent actually made (via `--ref`, resolved against the in-container cache, or `--from-file` from a `--output` export with a verified content hash), capped at 10 rows per upload. It persists them via the hidden `upsert_data_sample` MCP tool, which resolves `--catalog-path` tier-by-tier and walks each row's nested JSON keys into child KG nodes. Only `incidents` is a valid `--catalog-path` root — see [Resources](#resources).

## Edge-connectable fields

Scalar identity fields safe to use in `kg_learner.py create-edge`: an incident's `id`, `service.id`, `escalation_policy.id`, and (where present) an assignment's `assignee.id`/`assignee.email`. Do not edge on free-prose fields (`title`, `body.details`) — those aren't stable identifiers.

## Rendering

**Incidents (from `list-incidents`/`query`)** — default columns:

- Id — `id`
- Title — `title`
- Status — `status`
- Urgency — `urgency`
- Priority — `priority.summary` (may be null)
- Service — `service.summary`
- Created — `created_at`

Truncation: ≤5 rows render inline; >5 show the first 5 + "(N more, ask to see all)".

**Drill card (`get`)** — Id, Title, Status, Urgency, Priority, Service, Escalation Policy (`escalation_policy.summary`), Assignees (`assignments[].assignee.summary`), Created, Last Status Change (`last_status_change_at`).

**Services/escalation policies (from `list-services`/`list-escalation-policies`)** — Id, Name (`name`), Status (services only — `status`).

**On-calls (from `list-oncalls`)** — User (`user.summary`), Escalation Policy (`escalation_policy.summary`), Escalation Level (`escalation_level`), Start (`start`), End (`end` — null means the user doesn't go off-call).

After rendering, drop the raw JSON from reasoning.

## Error Handling

Distinct error classes, distinct fixes — don't conflate them:

- **401 Unauthorized** — the `api_key` is invalid, expired, or was revoked. Surface and stop; never retry. Prompt the operator to re-check/rotate the key in the connector config.
- **403 Forbidden on every write, while reads work** — the strongest signal the stored `api_key` is a **Read-only API Key** (an option on PagerDuty's own key-creation form). Nothing was changed. Tell the operator to create a full-access key instead and update the connector's `api_key` field; don't retry and don't substitute a different verb.
- **403 Forbidden on reads too** — the key's role lacks permission for the resource (e.g. a limited-scope key on an account with team-based restrictions). Ask the operator which role/team the key needs.
- **400 Bad Request on `create-incident`/`update-incident`** — usually an invalid `service`/`escalation_policy`/`priority` id, or (per PagerDuty's own error envelope, `{"error": {"code", "message", "errors": [...]}}`) a `From`-header/requester problem: `from_email` doesn't correspond to a real, active PagerDuty user on this account. The CLI surfaces the vendor's `error.message`/`error.errors`; resolve ids with `list-services`/`list-escalation-policies`/`list-priorities` first, or flag the `from_email` credential value to the operator. Nothing was written.
- **404 Not Found** — the incident/service/escalation-policy id doesn't exist or isn't visible to this key. For `get`/`update-incident`/`add-comment`, confirm the id with `list-incidents`/`list-services` rather than retrying the same id.
- **429 Too Many Requests** — rate limit hit (PagerDuty's documented limit is 960 requests/minute per token, independent of IP). Retry after a backoff; the response carries `ratelimit-limit`/`ratelimit-remaining`/`ratelimit-reset` headers if the raw response is inspectable.
- **Empty result on a 200** — a real, correct, empty result (the CLI prints `No <command> results (0 rows)`). Not a failure — don't retry. For `list-priorities` specifically, an empty result may mean the account hasn't enabled the Priorities feature rather than a filtering issue.
- **`update-incident` rejects combining `--escalation-policy` and `--assignee`** — this is PagerDuty's own constraint (an incident update can reassign to a policy's rotation *or* specific users, not both in one call), enforced client-side before the request is even sent. Pick one.

## Guardrails

- **`create-incident` fires immediately — no approval gate, unlike every other write in this repo.** This is deliberate: the connector's reason for existing is unattended escalation to PagerDuty when a customer has no 24/7 SOC watching (see the description frontmatter). Requiring a human to confirm each page would defeat that. What keeps this safe instead:
  - `--incident-key` is **required** (PagerDuty itself makes it optional) — always derive it deterministically from the correlated detection (e.g. a hash of the correlated alert ids), so a retried/duplicate call for the same detection lands on PagerDuty's own de-dup instead of paging someone twice.
  - `--service` is resolved by a direct `GET /services/{id}` before the incident is created — an id that doesn't exist fails clearly instead of the agent guessing or fuzzy-matching a name onto the wrong service.
  - Only call `create-incident` for a finding that actually meets the correlation/severity bar your broader workflow defines as "page-worthy" (e.g. multiple correlated alerts bumping a finding to critical priority) — this connector has no opinion on what counts as an alarm; that judgment belongs to whatever upstream logic decided to escalate.
- **`update-incident`/`add-comment` are gated behind `--yes` like every other write connector in this repo.** Never pass `--yes` on the first run — run without it, relay the exact mutation line to the user, and only re-run with `--yes` after explicit approval for that specific action.
- **Only the three listed writes.** No delete, no bulk operations, no schedule/user management. If the user needs one, say the connector doesn't do it.
- **Don't chain a gated write off your own read.** Finding 10 stale acknowledged incidents is not a mandate to resolve all 10; report the list and ask.
- **Writes are never retried automatically** — a re-sent `POST`/`PUT` would double the side effect (a second page, a duplicate note). If a write times out, its outcome is *unknown*; check with `get`/`list-incidents --incident-key <key>` before considering a second attempt, and say plainly that you're checking rather than assuming it failed.
- **Splunk (or the customer's SIEM/case-management connector) stays the system of record.** This connector doesn't create tickets or store investigation notes — it pages. Don't use `add-comment` as a substitute for real case documentation elsewhere.

## Connector

- **Base URL:** `https://api.pagerduty.com`.
- **Auth:** `Authorization: Token token=<api_key>` on every call; `From: <from_email>` on every call (required by PagerDuty on writes, ignored on reads) — both injected server-side.
- **Category:** `ticketing`.
- **Required runtime env:** `CROGL_MCP_URL`, `CROGL_CLI_TOKEN` (supplied by the agent container).
- **Output:** grouped PSV for lists (with a result `ref`), pretty-printed JSON for `get`/a write's response body, a one-line confirmation for an empty-body write response.
- **Write posture:** `create-incident` ungated (fires immediately); `update-incident`/`add-comment` gated behind `--yes`; no retries on any mutating request.
