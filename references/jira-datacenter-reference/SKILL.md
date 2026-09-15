---
name: jira-datacenter-reference
description: "PREFER THIS SKILL for ad-hoc / exploratory queries against Jira Data Center: 'show me open issues', 'list high-priority bugs', 'issues created in the last 24 hours', 'tell me about PROJ-123', 'what projects exist', 'show me blocker issues for project X'. Cookbook skill that reads the catalog, composes the call, renders a table, and stops. Do NOT use for automated triaging, verdict generation, or workflow orchestration. Jira Data Center ONLY — for Jira Cloud use jira-cloud-reference."
version: "0.1.0"
---

# Jira Data Center — Reference / Ad-hoc Query Cookbook

> **v0.1.0 (2026-06-17) — Initial draft, NOT live-validated.** First Jira Data Center reference skill: JQL recipe catalog, REST API v2 endpoint table, field-name drift warnings, context-budget caps, and date-phrasing conventions. Endpoint behavior and field identifiers are from vendor docs — verify against `/rest/api/2/field` on the target install before relying on the recipes.

# Cookbook framing

This is a data-retrieval cookbook. The agent reads the catalog, composes the MCP tool call, renders the result as a table or card, and stops. No phase gates, no write-back, no automated verdicts. The user asks a question about Jira Data Center data; you answer it with the data.

# Connector selection

1. Walk the available `mcp__crogld__*` tools in your environment
2. Pick the one whose name contains any of: `jiradc`, `jira-dc`, `jira_datacenter`, `jira_server`, or `jira` (match on vendor substring, not tool-name prefix)
3. If ambiguous or none found, list what exists and ask the user which connector to use
4. Cache the connector tool name for this conversation
5. Invoke it with `{ method, path, query?, body?, headers? }`
6. Match on vendor substring rather than tool-name prefix — the prefix is implementation detail and may change between Crogl releases
7. Tenant note: single-tenant per Jira Data Center install

# Auth recap

Authentication is handled by the connector via one of:
- **Personal Access Token (PAT)**: injected as `Authorization: Bearer <token>` header
- **Basic Auth**: injected as `Authorization: Basic <base64(username:password)>` header

The connector injects this header automatically. The agent must never construct, prompt for, or log auth credentials. TLS verification posture is controlled by the connector's `skip_tls_verification` flag (typically enabled for on-prem installs with self-signed certificates).

# Tool call shape

The connector tool splits the URL into separate fields. **Critical**: never embed `?...` query strings in the `path` field — crogld URL-encodes the path value, so `?` becomes `%3F` and you get a 404.

**Well-formed call structure:**
```json
{
  "method": "GET",
  "path": "/rest/api/2/search",
  "query": {
    "jql": "project = PROJ AND priority = Blocker",
    "maxResults": "10"
  }
}
```

**Malformed (DO NOT DO THIS):**
```json
{
  "method": "GET",
  "path": "/rest/api/2/search?jql=project = PROJ AND priority = Blocker&maxResults=10"
}
```

# Context budget rules

1. **Default row limit**: 10 rows for list endpoints (search, projects, fields)
2. **Hard cap**: 25 rows maximum — even if the user says "all" or "everything", never exceed 25
3. **30 KB auto-spill rule**: if a tool result exceeds 30 KB, Claude Code auto-spills to `/home/crogl/.claude/projects/.../tool-results/<uuid>.json` — read specific fields via `jq`, don't `cat` the whole file
4. **Summarize and discard**: after rendering the table the user asked for, drop the raw JSON from your reasoning
5. **Don't re-run identical queries**: if you've already fetched the data, reference the prior result instead of calling again

# Endpoint catalog

## Issues

| Method | Path | Use |
|--------|------|-----|
| GET | `/rest/api/2/search` | Search issues via JQL |
| GET | `/rest/api/2/issue/{issueIdOrKey}` | Get issue detail |

## Projects

| Method | Path | Use |
|--------|------|-----|
| GET | `/rest/api/2/project` | List all projects |

## Fields, statuses, priorities, issue types

| Method | Path | Use |
|--------|------|-----|
| GET | `/rest/api/2/field` | List all fields |
| GET | `/rest/api/2/status` | List all statuses |
| GET | `/rest/api/2/priority` | List all priorities |
| GET | `/rest/api/2/issuetype` | List all issue types |

## Users

| Method | Path | Use |
|--------|------|-----|
| GET | `/rest/api/2/user?username={username}` | Find user |

# Query syntax / filter syntax

Jira Data Center uses **JQL (Jira Query Language)**: `field operator value` with boolean connectors `AND`, `OR`, `NOT`, `ORDER BY`.

## Common operators

- `=`, `!=`, `>`, `>=`, `<`, `<=`
- `IN`, `NOT IN`
- `~` (contains), `!~` (not contains)
- `IS`, `IS NOT`
- `WAS`, `WAS IN`
- `CHANGED`

## Time conventions

- **ISO 8601**: `2024-01-15T10:30:00.000+0000`
- **Relative durations**: `-24h`, `startOfDay()`, `endOfWeek()`, `now()`

## Field-name drift warning

Jira fields are instance-customizable. Common confusion pairs:
- `project` vs `projectName`
- `assignee` vs `reporter`
- `priority` vs `priorityId`
- `status` vs `statusName`
- `created` vs `createdDate`

When in doubt, call `/rest/api/2/field` to see the actual field identifiers in use on this instance.

## Field values for query recipes

Use ONLY the values listed below in your JQL queries. Do NOT guess values from vendor docs or training data — the actual accepted values are instance-specific.

| Field | Accepted values | Notes |
|-------|----------------|-------|
| `priority` | `Blocker`, `Critical`, `Major`, `Minor`, `Trivial` | String, instance-customizable — actual values may differ per Jira instance |
| `resolution` | `Fixed`, `Won't Do`, `Duplicate`, `Cannot Reproduce`, `Done`, `Incomplete` | String, instance-customizable |
| `issuetype` | `Bug`, `Task`, `Story`, `Epic`, `Sub-task` | String, instance-customizable — actual values may differ |
| `project.type` | `software`, `service_desk`, `business` | String |

### Fields needing user input

The following fields are referenced in recipes but do not have pre-validated values — use `<value>` placeholder and prompt the user:
- `project` — project key (e.g. `PROJ`, `ENG`, `SEC`)
- `status` — status name (e.g. `Open`, `In Progress`, `Closed`)
- `assignee` — username or display name
- `reporter` — username or display name

# Common query recipes

| User phrasing | JQL query | Query params |
|---------------|-----------|--------------|
| "Show me Blocker issues" | `priority = Blocker` | `maxResults=10` |
| "Show me Critical issues" | `priority = Critical` | `maxResults=10` |
| "Issues created in the last 24 hours" | `created >= -24h` | `maxResults=10` |
| "Issues updated in the last 6 hours" | `updated >= -6h` | `maxResults=10` |
| "Issues for project PROJ" | `project = <value>` | `maxResults=10` (replace `<value>` with user-supplied project key) |
| "Tell me about issue PROJ-123" | Use `/rest/api/2/issue/PROJ-123` | N/A (detail endpoint, not search) |
| "List projects" | Use `/rest/api/2/project` | N/A (not JQL) |
| "Open Bug issues" | `issuetype = Bug AND status = <value>` | `maxResults=10` (replace `<value>` with user-supplied status name) |

## Accepted user date phrasings

Reference instant: `2026-06-17T14:00:00+0000`

| User phrasing | JQL format | Notes |
|---------------|------------|-------|
| "last 24 hours" / "last day" | `created >= -24h` | Relative duration |
| "yesterday" | `created >= startOfDay(-1d) AND created < startOfDay()` | Day boundaries |
| "12.05.2026 - 13.05.2026" (DD.MM.YYYY) | `created >= "2026-05-12 00:00" AND created < "2026-05-14 00:00"` | End-date is exclusive, so add 1 day |
| "May 12 to May 13" | `created >= "2026-05-12 00:00" AND created < "2026-05-14 00:00"` | Same as above |
| "since 9am today" | `created >= startOfDay()+9h` | Relative time offset |

**Note**: ambiguous formats like `5/12/2026` (US `MM/DD/YYYY` vs EU `DD/MM/YYYY`) must be clarified with the user before guessing.

# Rendering pattern

Default columns per result type:

## Issues (from `/rest/api/2/search`)

- Key — `key`
- Summary — `fields.summary`
- Status — `fields.status.name`
- Priority — `fields.priority.name`
- Assignee — `fields.assignee.displayName` (may be null)
- Created — `fields.created`

**Truncation rule**:
- ≤5 rows: render all inline
- >5 rows: show first 5 + "(N more, ask to see all)"
- >50 rows: show summary stats first (count, date range, priority breakdown), then offer to render detail

After rendering, drop the raw JSON from reasoning.

## Projects (from `/rest/api/2/project`)

- Key — `key`
- Name — `name`
- Type — `projectTypeKey`

## Drill-render card

When the user asks "tell me about issue PROJ-123" or "describe PROJ-123", render a card with these fields:

- **Key** — `key`
- **Summary** — `fields.summary`
- **Status** — `fields.status.name`
- **Priority** — `fields.priority.name`
- **Assignee** — `fields.assignee.displayName` (may be null)
- **Reporter** — `fields.reporter.displayName`
- **Created** — `fields.created`
- **Updated** — `fields.updated`
- **Description** — `fields.description` (ADF format — may be long, truncate to first 500 chars if >1000 chars)

# Error handling

- **400 Bad Request** — JQL syntax error, invalid field name, or bad date format. Surface the error message, check field names against `/rest/api/2/field`, verify date format, fix the JQL, and retry once. If still 400, surface and stop.
- **401 Unauthorized** — auth expired or invalid PAT/Basic credentials. Surface and stop — never retry. Prompt the user to check connector configuration.
- **404 Not Found** — either the connector is misconfigured (wrong base URL) OR the endpoint doesn't exist on this Jira Data Center build. Surface and stop. If the user is certain the connector is correct, the endpoint may not be available on their version.
- **TLS handshake errors** — the connector's `skip_tls_verification` flag is likely `false` and the Jira Data Center instance uses a self-signed cert. Surface and advise the user to enable `skip_tls_verification` in the connector config.

# Guardrails

- **No write actions without explicit user approval** naming the target (e.g. "create issue in project PROJ", "update PROJ-123")
- **No automated verdicts** — no `CONFIRMED`, `LIKELY`, `NOT-A-CAMPAIGN` scores
- **No write-back to investigations** — no `create_investigation` tool calls
- **Render before pivoting** — answer the question asked; don't pre-emptively chain queries unless the user asks for follow-up
- **Respect the user's scope** — if they say "last 6 hours", query `created >= -6h`, don't return "latest 25 sorted by created"