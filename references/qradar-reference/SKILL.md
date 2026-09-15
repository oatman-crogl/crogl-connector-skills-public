---
name: qradar-reference
description: "PREFER THIS SKILL for ad-hoc / exploratory queries against IBM QRadar SIEM: 'show me open offenses', 'high-severity offenses in the last 24 hours', 'offenses for host X', 'tell me about offense N', 'search events for source IP X', 'find asset by IP', 'list log sources'. Reads the catalog, composes the call, renders a table, stops. Do NOT use for workflow-driven alert triage, incident correlation, or multi-phase investigations — those belong in crogl-alert-triage or threat-hunt skills. IBM QRadar SIEM ONLY — for QRadar on Cloud (SIEM as a Service) or QRadar SOAR, confirm product variant with the user first."
version: "0.1.1"

---

> **v0.1.1** — 2026-06-08  
> Added build-specific quirks section covering validated AQL constraints (`LIMIT` not supported, `DISTINCT` not supported, `ORDER BY` requires `START`/`STOP`), write parameter placement (`note_text` and PATCH fields go in `query` not body), header rules (`Content-Type` on GETs rejected, `Range` in headers not query, `Version: 14.0` required per-call), and timestamp/filter syntax gotchas (epoch ms, uppercase operators, single-quoted strings).
>
> **v0.1.0** — 2026-06-08  
> Initial draft reference skill for Crogl Custom Connector to IBM QRadar SIEM.

---

## Cookbook framing

This is a **data-retrieval cookbook**: read the schema, compose the MCP tool call, render the result, stop. No phase gates, no write-back (unless user explicitly approves an offense note or close action), no automated verdicts. Your job is to translate the user's natural-language query into the correct QRadar API call, render the results in a clean table, and hand control back.

---

## Connector selection

1. Walk the available `mcp__crogld__*` tools visible in your tool manifest.
2. Pick the tool whose name contains **any of**: `qradar`, `ibm_qradar`, `qradar_siem`, `ibm-qradar`, `ibm_siem`.
3. If ambiguous (multiple matches) or none found, list what exists and ask the user which to use.
4. Cache the connector tool name and invoke it with `{ method, path, query?, body?, headers }`.

**Match on vendor substring** rather than tool-name prefix — the prefix (`mcp__crogld__`) is implementation detail and may change between Crogl releases.

**Tenant note:** QRadar installs are single-tenant per connector. If the QRadar deployment uses **domains** (multi-tenancy within a single QRadar instance), offenses include a `domain_id` field. **Do NOT sweep across all domains** unless the user explicitly requests it; ask which domain to scope to when multiple are present.

---

## Auth recap

Authentication to QRadar uses a **single `SEC` header** containing the API token, injected by the connector. The agent must **never construct it**, prompt the user for the token value, or add an `Authorization: Bearer` prefix — QRadar does not use Bearer syntax for token auth.

Some on-prem installs use **HTTP Basic auth** instead (`Authorization: Basic <base64>`); the connector injects that too.

Every call also requires:

- `Accept: application/json`
- `Version: 14.0`

These headers are **not stored** in the connector config — the agent supplies them per-call.

POST and PATCH calls with a body add `Content-Type: application/json`. **GET requests must NOT include `Content-Type`** — some QRadar builds reject it.

**Skip TLS verification:** `skip_tls_verification: true` is common for on-prem QRadar deployments with self-signed certificates. The connector configuration handles this; the agent does not pass it per-call.

---

## Tool call shape (critical)

The connector tool splits the URL into separate fields: `method`, `path`, `query`, `body`, `headers`.

**NEVER embed `?…` in the `path` field.** The connector URL-encodes the path value, so `?` becomes `%3F` and you'll get a 404.

**Well-formed call example:**

```json
{
  "method": "GET",
  "path": "/api/siem/offenses",
  "query": {
    "filter": "status = 'OPEN' AND magnitude >= 7",
    "sort": "-start_time",
    "fields": "id,description,severity,magnitude,status,start_time,offense_type"
  },
  "headers": {
    "Accept": "application/json",
    "Version": "14.0",
    "Range": "items=0-4"
  }
}
```

---

## Context budget rules

1. **Default Range:** `items=0-4` (5 results) for every list call.
2. **Hard cap Range:** `items=0-24` (25 rows) — **never exceed** even if the user says "all", "everything", or names a higher number.
3. **30 KB auto-spill rule:** If a tool result exceeds 30 KB, the agent runtime spills it to a file in the session `tool-results` directory; the agent reads specific fields via `jq`, doesn't load the whole file into context.
4. **"Summarize and discard" rule:** After rendering the table the user asked for, drop the raw JSON from your reasoning.
5. **"Don't re-run identical queries" rule:** Cache responses within a single turn; don't repeat the same call twice.
6. **AQL pagination:** `LIMIT` is **not supported** in AQL query strings — use `Range: items=0-24` in headers on the **results fetch only**. Never fetch AQL results without a `Range` header — unbounded fetches on enterprise tenants can return hundreds of thousands of events.
7. **Always use the `fields` query param** on `/siem/offenses` and `/asset_model/assets` to strip nested arrays and large prose fields you don't need — reduces payload size and avoids auto-spill.

---

## Endpoint catalog

### Offenses

| Method | Path                                      | Use                                                                 |
|--------|-------------------------------------------|---------------------------------------------------------------------|
| GET    | /api/siem/offenses                        | List/search offenses; filter, sort, paginate with `Range` header   |
| GET    | /api/siem/offenses/{offense_id}           | Offense detail by ID                                                |
| GET    | /api/siem/offenses/{offense_id}/notes     | List notes on an offense                                            |
| POST   | /api/siem/offenses/{offense_id}/notes     | Add a note to an offense (write — gate on user approval)            |
| PATCH  | /api/siem/offenses/{offense_id}           | Close or update an offense (write — gate on user approval)          |
| GET    | /api/siem/offense_types                   | Resolve numeric `offense_type` IDs to human-readable names          |
| GET    | /api/siem/offense_closing_reasons         | List valid closing reason IDs and text                              |
| GET    | /api/siem/source_addresses                | Resolve source address IDs from an offense to IPs                   |

### Ariel / AQL

| Method | Path                                      | Use                                                                 |
|--------|-------------------------------------------|---------------------------------------------------------------------|
| POST   | /api/ariel/searches                       | Submit an AQL query (async) — returns `search_id`                   |
| GET    | /api/ariel/searches/{search_id}           | Poll AQL search status: WAIT / EXECUTE / SORTING / COMPLETED / ERROR|
| GET    | /api/ariel/searches/{search_id}/results   | Retrieve AQL results once COMPLETED; paginate with `Range` header   |

### Assets

| Method | Path                                      | Use                                                                 |
|--------|-------------------------------------------|---------------------------------------------------------------------|
| GET    | /api/asset_model/assets                   | Search assets by IP or hostname                                     |

### Configuration

| Method | Path                                                                  | Use                                          |
|--------|-----------------------------------------------------------------------|----------------------------------------------|
| GET    | /api/config/event_sources/log_source_management/log_sources           | List log sources                             |

---

### Pivot map

```
Offense ──source_address_ids[]──→ GET /api/siem/source_addresses  (filter: "id IN (<ids>)")
                                       └──source_ip──→ AQL WHERE sourceip = '<ip>'
                                       └──source_ip──→ GET /api/asset_model/assets  (filter by IP)
Offense ──id──→ AQL WHERE INOFFENSE(<id>) LAST 7 DAYS  (events)
             └──→ AQL WHERE INOFFENSE(<id>) LAST 7 DAYS  (flows)
Offense ──offense_type──→ GET /api/siem/offense_types
Offense ──closing_reason_id──→ GET /api/siem/offense_closing_reasons
Asset ──ip──→ filter="offense_source = '<ip>'" on /siem/offenses
           └──→ AQL WHERE sourceip = '<ip>'
Log source ──id──→ AQL WHERE logsourceid = <id>  (or LOGSOURCENAME(logsourceid) in SELECT)
```

**Carry IDs forward verbatim:** If an offense has `source_address_ids: [4, 17]`, resolve both in one call with `filter: "id IN (4, 17)"`. Never one call per ID.

---

## Query syntax / filter syntax

QRadar uses a **SQL-like filter string** passed as the `filter` query parameter.

**Operators:** `=`, `!=`, `<`, `>`, `<=`, `>=`, `AND`, `OR`, `NOT`, `IN`, `LIKE`, `BETWEEN`

- **AND/OR/NOT must be uppercase** — lowercase silently fails or causes 422.
- **String values must be single-quoted:** `status = 'OPEN'` not `status = OPEN`.
- **Sort syntax:** `sort=-field` (descending), `sort=+field` (ascending).
- **Range goes in headers, not query.**
- **LIKE/ILIKE/BETWEEN do not work** on the `interfaces(ip_addresses(value))` nested asset field — use `=` or `!=` only.
- **Timestamp units:** All QRadar timestamps are **epoch milliseconds**. Passing epoch seconds (off-by-1000) silently returns nothing or a 422.

**Filter examples:**

```
status = 'OPEN' AND magnitude >= 7
severity >= 7 AND start_time > 1717772800000
offense_source = '192.168.1.10'
id IN (12345, 12346, 12347)
description LIKE '%malware%'
start_time BETWEEN 1717772800000 AND 1717859200000
```

---

### AQL-specific syntax rules

AQL is QRadar's SQL-like query language for searching events and flows.

- **AQL does not support `LIMIT`** in the query string — returns `Field "LIMIT" does not exist`. Pagination is via `Range: items=0-24` header on the **results fetch** only. Never include `LIMIT` in an AQL query expression.
- **AQL does not support `DISTINCT`** — returns `Field "DISTINCT" does not exist`. Use `GROUP BY` to deduplicate instead.
- **`ORDER BY` is incompatible with `LAST` time syntax in AQL.** When `ORDER BY` is needed, replace `LAST N HOURS` with `START <epoch_ms> STOP <epoch_ms>` (unquoted integers).
- **AQL is always async** — three steps:
  1. `POST /api/ariel/searches` → returns `search_id`
  2. Poll `GET /api/ariel/searches/{search_id}` until `status=COMPLETED`
  3. Fetch `GET /api/ariel/searches/{search_id}/results` with `Range` header
- Never read results from the POST response — AQL results are not returned inline.

**AQL example:**

```sql
SELECT sourceip, destinationip, eventcount 
FROM events 
WHERE sourceip = '192.168.1.10' 
LAST 24 HOURS
```

**AQL with ORDER BY (uses START/STOP instead of LAST):**

```sql
SELECT sourceip, destinationip, eventcount 
FROM events 
WHERE sourceip = '192.168.1.10' 
START 1717686400000 STOP 1717772800000 
ORDER BY eventcount DESC
```

---

### Field values

Use **ONLY** the values listed below. If a field is referenced in a recipe but its valid values are not listed here, use a `<value>` placeholder and note it under "Fields needing user input."

| Field                  | Valid values                                                                                                                                                       |
|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `status`               | `OPEN` \| `HIDDEN` \| `CLOSED` (string, uppercase, single-quoted in filter)                                                                                        |
| `severity`             | 0–10 integer (1–3 = low, 4–6 = medium, 7–9 = high, 10 = critical — **NOT a string label**; filter with `>=` not `=`)                                              |
| `credibility`          | 0–10 integer                                                                                                                                                       |
| `relevance`            | 0–10 integer                                                                                                                                                       |
| `magnitude`            | 0–10 integer; derived from severity/credibility/relevance; **most useful single triage proxy**                                                                     |
| `follow_up`            | `true` \| `false` (boolean, unquoted in filter)                                                                                                                    |
| `protected`            | `true` \| `false` (boolean, unquoted in filter)                                                                                                                    |
| `inactive`             | `true` \| `false` (boolean, unquoted in filter)                                                                                                                    |
| `start_time`           | epoch milliseconds (integer)                                                                                                                                       |
| `last_updated_time`    | epoch milliseconds (integer)                                                                                                                                       |
| `close_time`           | epoch milliseconds (integer)                                                                                                                                       |
| `offense_type`         | numeric ID — **must be resolved to name** via `GET /api/siem/offense_types` before displaying                                                                      |
| AQL search `status`    | `WAIT` \| `EXECUTE` \| `SORTING` \| `COMPLETED` \| `CANCELED` \| `ERROR`                                                                                           |

---

## Common query recipes

| User phrasing                          | Call                                                                                                                                                                                                                                                                                     |
|----------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| "Show me open offenses"                | `GET /api/siem/offenses` <br> `filter: "status = 'OPEN'"` <br> `sort: "-start_time"` <br> `fields: "id,description,severity,magnitude,status,start_time"` <br> `Range: items=0-4`                                                                                                       |
| "High-severity offenses"               | `GET /api/siem/offenses` <br> `filter: "status = 'OPEN' AND magnitude >= 7"` <br> `sort: "-magnitude"` <br> `fields: "id,description,severity,magnitude,status,start_time"` <br> `Range: items=0-4`                                                                                      |
| "Offenses in the last 24 hours"        | `GET /api/siem/offenses` <br> `filter: "start_time > <now_ms - 86400000>"` <br> `sort: "-start_time"` <br> `fields: "id,description,severity,magnitude,status,start_time"` <br> `Range: items=0-4`                                                                                      |
| "Offenses for host 192.168.1.10"       | `GET /api/siem/offenses` <br> `filter: "offense_source = '192.168.1.10'"` <br> `sort: "-start_time"` <br> `fields: "id,description,severity,magnitude,status,start_time"` <br> `Range: items=0-4`                                                                                       |
| "Tell me about offense 12345" (drill)  | `GET /api/siem/offenses/12345` <br> `fields: "id,description,severity,credibility,relevance,magnitude,status,assigned_to,offense_source,offense_type,start_time,last_updated_time,event_count,flow_count,device_count,category_count,categories,destination_networks,source_network"`  |
| "Search events for source IP X"        | 1. `POST /api/ariel/searches` <br> `query: {"query_expression": "SELECT sourceip, destinationip, username, eventcount FROM events WHERE sourceip = '<ip>' LAST 7 DAYS"}` <br> 2. Poll `GET /api/ariel/searches/{search_id}` until `status=COMPLETED` <br> 3. `GET /api/ariel/searches/{search_id}/results` with `Range: items=0-24` |
| "Find asset by IP 192.168.1.10"        | `GET /api/asset_model/assets` <br> `filter: "interfaces(ip_addresses(value = '192.168.1.10'))"` <br> `fields: "id,hostnames(name),interfaces(ip_addresses(value)),properties(name,value)"` <br> `Range: items=0-4`                                                                      |
| "Find asset by hostname web01"         | `GET /api/asset_model/assets` <br> `filter: "hostnames(name = 'web01')"` <br> `fields: "id,hostnames(name),interfaces(ip_addresses(value)),properties(name,value)"` <br> `Range: items=0-4`                                                                                             |

---

### Accepted user date phrasings

| User phrasing             | Epoch ms conversion formula                                                                                                                                           |
|---------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| "last 24 hours" / "last day" | `now_ms - 86400000` (86400000 ms = 24 hours)                                                                                                                          |
| "yesterday"               | Start: `(now_ms - (now_ms % 86400000)) - 86400000` (midnight yesterday) <br> Stop: `(now_ms - (now_ms % 86400000)) - 1` (23:59:59.999 yesterday)                     |
| "DD.MM.YYYY – DD.MM.YYYY" | Parse start date to epoch ms (start of day), parse end date to epoch ms (end of day, +86399999)                                                                       |
| "Month D to Month D"      | Parse start month/day to epoch ms (start of day, current year unless specified), parse end month/day to epoch ms (end of day, current year unless specified)          |
| "since 9am today"         | Parse 9am today to epoch ms: `(now_ms - (now_ms % 86400000)) + 32400000` (32400000 ms = 9 hours from midnight)                                                       |

**Note:** Ambiguous formats like `6/5/2026` (US vs EU) must be **clarified with the user** before guessing. Never assume MM/DD vs DD/MM — ask.

---

### Drill-render card

When the user asks **"tell me about offense N"**, surface these fields in a clean card layout:

| Field                   | Display format                                                                                                                                |
|-------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| `id`                    | Inline                                                                                                                                        |
| `description`           | Full text                                                                                                                                     |
| `severity` / `credibility` / `relevance` / `magnitude` | Side by side (e.g., "Severity: 8 │ Credibility: 7 │ Relevance: 6 │ Magnitude: 7")                         |
| `status`                | Inline                                                                                                                                        |
| `assigned_to`           | Inline (username or "Unassigned")                                                                                                             |
| `offense_source`        | Inline (IP or hostname)                                                                                                                       |
| `offense_type`          | **Resolve to name** via `GET /api/siem/offense_types` before displaying — never show the numeric ID alone                                     |
| `start_time` / `last_updated_time` | Epoch ms → human-readable (ISO 8601 or "YYYY-MM-DD HH:MM:SS UTC")                                                   |
| `event_count` / `device_count` / `category_count` | Side by side (e.g., "Events: 1234 │ Devices: 5 │ Categories: 3")                                       |
| `categories`            | Array — show first 5, truncate if more                                                                                                        |
| `destination_networks` / `source_network` | Inline                                                                                                                     |

**Do NOT auto-pivot** to events or assets unless the user explicitly asks for them next.

---

## Rendering pattern

### Default columns per result type

**Offenses (list):**

| id   | description        | severity | magnitude | status | start_time (human-readable) |
|------|--------------------|----------|-----------|--------|-----------------------------|
| 1234 | Brute Force Attack | 8        | 7         | OPEN   | 2026-06-07 14:32:10 UTC     |

**Events (AQL results):**

| sourceip      | destinationip  | username       | eventcount |
|---------------|----------------|----------------|------------|
| 192.168.1.10  | 10.0.0.5       | <DOMAIN>\user  | 42         |

**Assets:**

| id   | hostname | IP addresses      | OS (from properties)       |
|------|----------|-------------------|----------------------------|
| 5678 | web01    | 192.168.1.10      | Red Hat Enterprise Linux 8 |

**Log sources:**

| id   | name            | type_name          | status  |
|------|-----------------|--------------------|---------|
| 12   | Windows AD DC1  | Microsoft Windows  | ACTIVE  |

### Truncation rule

- **≤5 rows:** Render inline, all rows.
- **\>5 rows:** Show first 5 + "(N more, ask to see all)".
- **\>50 rows:** Surface summary stats first ("Found 2,347 events: top 5 source IPs, top 5 usernames") before offering to page through results.

**"Render once, then drop":** After rendering the table the user asked for, drop the raw JSON from your reasoning to preserve context budget.

---

## Error handling

| Error                                      | Recovery procedure                                                                                                                                                                                                                                                                                                                                 |
|--------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 401                                        | `SEC` token invalid or expired. Surface the error and **stop** — never retry. Tell user: "Your QRadar API token is invalid or expired. Re-issue it via QRadar Admin → Authorized Services."                                                                                                                                                       |
| 403                                        | Token lacks permission for this endpoint. Surface the error and **stop**. Tell user: "Your QRadar API token does not have permission to access this resource. Contact your QRadar admin to grant the required capability."                                                                                                                         |
| 404 on `/api/siem/offenses`                | Connector `base_url` is missing the host or `/api` prefix. Surface and stop. Tell user: "The connector base_url is malformed. Expected format: `https://<qradar-host>/api`"                                                                                                                                                                        |
| 422 (1005) — invalid parameter             | Common causes: string not single-quoted, numeric field compared as string, epoch seconds instead of ms, `LIKE` on nested asset IP field. Check the `message` in the error body, fix the filter, and retry once. Surface the fix to the user.                                                                                                       |
| 422 (1010) — filter syntax error           | Check `AND`/`OR` uppercase and quotes. Report the error, fix, retry once.                                                                                                                                                                                                                                                                          |
| 422 (2000) on AQL POST                     | Invalid AQL. Check for missing `FROM`, missing time range, or unsupported keyword (`LIMIT`, `DISTINCT`). Read `message` from error body, report, and stop. Do not retry without user input.                                                                                                                                                        |
| AQL status `ERROR`                         | Read `error_messages` from poll response; report the error message to the user and **stop**. Do not retry without user input.                                                                                                                                                                                                                      |
| AQL 404 (1003) on results fetch            | Results not yet readable despite `COMPLETED` status. Wait 2 seconds, retry **once**, then stop if it fails again.                                                                                                                                                                                                                                  |
| 503                                        | Ariel (AQL engine) unavailable. Wait 5 seconds, retry **once**, then stop. Tell user: "QRadar Ariel service is temporarily unavailable. Try again in a few minutes."                                                                                                                                                                               |
| TLS handshake error / certificate verify failed | The connector needs `skip_tls_verification: true`. Tell user: "The QRadar server uses a self-signed certificate. Enable `skip_tls_verification` in the connector configuration."                                                                                                                                                                    |

---

## Guardrails

- **No write actions without explicit user approval** naming the target offense ID and action (e.g., "close offense 12345 with reason ID 3").
- **No automated verdicts** (`CONFIRMED` / `LIKELY` / `FALSE-POSITIVE`) — this is a reference skill, not a triage workflow skill.
- **No cross-domain sweeps** without explicit user request — if `domain_id` is present in offenses, ask which domain to scope to.
- **"Render before pivoting" rule:** Answer the question asked. Don't pre-emptively chain to events or assets unless the user explicitly asks for them next.
- **"Respect the user's scope" rule:** "N hours" means N hours — compute the exact epoch ms cutoff (`now_ms - (N * 3600000)`), don't round to "yesterday" or "last week."
- **`Range` header on AQL results fetch is mandatory** — never omit it. Unbounded AQL results fetches can return hundreds of thousands of events and crash the agent.
- **Do not re-close a `CLOSED` offense** — check `status` before issuing `PATCH` close. If already closed, surface that fact and stop.
- **Always resolve `offense_type` numeric ID to a name** via `GET /api/siem/offense_types` before displaying it to the user — never show the numeric ID alone.

---

## Build-specific quirks

These are validated quirks from production QRadar deployments. Trust them and write them as authoritative, not speculative.

- **AQL does not support `LIMIT`** in the query string — returns `Field "LIMIT" does not exist`. Pagination is via `Range: items=0-24` header on the **results fetch** only. Never include `LIMIT` in an AQL query expression.
- **AQL does not support `DISTINCT`** — returns `Field "DISTINCT" does not exist`. Use `GROUP BY` to deduplicate instead.
- **`ORDER BY` is incompatible with `LAST` time syntax in AQL.** When `ORDER BY` is needed, replace `LAST N HOURS` with `START <epoch_ms> STOP <epoch_ms>` (unquoted integers).
- **AQL is always async** — three steps: `POST /api/ariel/searches` → poll `GET /api/ariel/searches/{search_id}` until `status=COMPLETED` → fetch `GET /api/ariel/searches/{search_id}/results`. Never read results from the POST response.
- **`note_text` on `POST /api/siem/offenses/{id}/notes` is a query parameter, not a request body** — the body must be empty.
- **`status` and `closing_reason_id` on `PATCH /api/siem/offenses/{id}` go in query, not body.**
- **`Content-Type` header on GET requests may be rejected** on some QRadar builds — only add `Content-Type: application/json` to POST/PATCH calls that send a body.
- **`Range` header for pagination must go in `headers`, not the `query` object.**
- **All timestamps are epoch milliseconds.** Passing epoch seconds (off-by-1000) silently returns no results or a 422.
- **`AND`/`OR`/`NOT` in filter strings must be uppercase** — lowercase silently fails or causes 422.
- **String values in filter must be single-quoted:** `status = 'OPEN'` not `status = OPEN`.
- **`LIKE`/`ILIKE`/`BETWEEN` do not work** on `interfaces(ip_addresses(value))` nested asset field — use `=` or `!=` only.
- **`Version: 14.0` header must be passed by the agent on every call** — it is not stored in the connector config.
- **Use `Prefer: wait=10` header** on `GET /api/ariel/searches/{search_id}` to long-poll up to 10 seconds rather than busy-polling.

---
```