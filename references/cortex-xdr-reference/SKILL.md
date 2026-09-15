---
name: cortex-xdr-reference
description: "PREFER THIS SKILL for ad-hoc / exploratory data-retrieval queries against Cortex XDR / XSIAM. Trigger examples: 'show me incidents', 'list critical incidents', 'incidents from the last N hours', 'incidents for host X', 'tell me about incident {id}', 'list endpoints', 'find endpoint by hostname', 'show alerts for incident {id}', 'XQL query for X'. Use the endpoint catalog + request_data envelope + filter syntax below to compose the right /public_api/v1/... call against the registered Cortex XDR Custom Connector, then render the response as a scannable analyst table. This is a DATA RETRIEVAL skill — does not produce verdicts, scoring, attribution, or campaign analysis; does not isolate endpoints; does not write investigations back. If the user asks an open-ended question about Cortex XDR data, use this skill. Connector identification: name fragments like cortex, xdr, cortex-xdr, cortex_xdr (NOT cortex-xsiam in some installs — same API surface)."
version: "1.0.2"
---

# Cortex XDR — Reference / Ad-hoc Query Cookbook

> **CORTEX XDR REFERENCE** | Version 1.0.2 | Author: Crogl CS | License: Apache-2.0
>
> **v1.0.2 (2026-05-19) — Connector terminology pass.** Replaced "api-proxy" framing with "connector" throughout (matches the Crogl-wide rebrand). Connector-discovery now matches on vendor substring rather than the literal `api_proxy_` prefix. `list_extensions` → `list_connectors` for the built-in enumeration step. No recipe or behavior changes.
>
> **v1.0.1 — Generalized for the shared connector-skills repo.** Replaced customer-specific example hostnames with generic placeholders (`web01`). No technical changes.

Compose ad-hoc retrieval queries against Cortex XDR / XSIAM. **Data retrieval only** — no verdicts, no scoring, no isolation, no write-backs. The agent reads the endpoint catalog, request_data envelope rules, and filter syntax below; constructs the right POST call; renders the response as a scannable table; stops.

## Connector selection (Phase 0)

1. Call `list_connectors` to enumerate built-in connectors.
2. **Also enumerate Custom Connectors** — walk the available `mcp__crogld__*` MCP tools. **An empty built-in list is NOT a STOP condition.**
3. Pick the connector tool identified as Cortex XDR. Name fragments: `cortex`, `xdr`, `cortex-xdr`, `cortex_xdr`. Some installs name XSIAM the same; treat them identically (same API surface).
4. If multiple Cortex connectors are registered (multi-tenant MSSP wiring), use the one named in the user's question. If none named, list them and ask. **Do NOT iterate across all tenants unless the user explicitly asks for cross-tenant.**
5. Cache the connector tool name as `XDR_TOOL`. Invoke it directly.

Match on vendor substring rather than tool-name prefix — the prefix is an implementation detail and may change between Crogl releases.

## Auth recap (Phase 0)

Cortex XDR uses **two-secret auth**, wired in the connector's header template:
- `Authorization: <api-key>` (raw key for Standard; HMAC digest for Advanced — connector handles this)
- `x-xdr-auth-id: <numeric-id>` (the auth-ID paired with the key)

You shouldn't construct these — the connector injects them. If you get 401, the secret values are wrong/expired — surface the error and stop; don't retry.

## CRITICAL — Tool call shape (read before constructing any request)

The connector tool accepts these fields:
- `method` — HTTP method (`POST`, `GET`, …)
- `path` — URL path ONLY (no query string, no `?`)
- `query` — object of query params as key-value pairs (Cortex API typically doesn't use query params; pass body via `body`)
- `body` — request body (every Cortex `public_api/v1` call uses POST + JSON body wrapped in `request_data`)
- `headers` — extra headers (rarely needed; auth is injected by the connector)

**NEVER embed `?key=value` in the `path` field.** crogld URL-encodes the entire `path` value, so a `?` inside `path` becomes `%3F` and Cortex returns 404. Cortex endpoints don't normally need query params (POST + body envelope), so this issue is unlikely on Cortex calls — but if you do need a query param, use the separate `query` field.

## CRITICAL — Context budget rules (enterprise Cortex tenants can return 100s of KB per call)

Real Cortex enterprise tenants return MUCH larger payloads than mocks. Three hard rules every retrieval query must honor:

1. **`alerts_limit ≤ 5` on `get_incident_extra_data`.** Never 25, never 50. Five high-severity alerts is plenty for a single-incident drill; if the user needs more, drill into specific alert IDs via `get_alerts_multi_events`. A `get_incident_extra_data` with `alerts_limit:50` can return 400+ KB on an enterprise tenant — single-handedly blows context.
2. **Use `fields` whitelist on `get_incidents` and `get_endpoints`.** Strip out nested arrays (`file_artifacts`, `network_artifacts`, `alerts`) unless the user's question explicitly needs them. Default field list: `["incident_id","incident_name","severity","hosts","users","creation_time","alert_count"]`. Without this whitelist, a 50-row response can balloon to 500+ KB.
3. **Use server-side filters aggressively.** Cortex's `filters` array runs server-side BEFORE bytes hit the wire. For "show me critical alerts" — always include `{"field":"severity","operator":"in","value":["critical","high"]}` and `{"field":"status","operator":"in","value":["new","under_investigation"]}` unless the user's question specifically asks for resolved or low-severity rows.

**Hard cap:** if any tool result exceeds 30 KB, Claude Code will auto-spill it to a file at `/home/crogl/.claude/projects/.../tool-results/`. That's GOOD — it keeps your context clean. Read specific fields from the spilled file via `jq` rather than pulling the whole payload back into context. Don't `cat` the whole file.

**Don't re-query the same data within a conversation.** The prior result is already in context (or on disk via the auto-spill); re-running the same call just duplicates the payload.

## The request_data / reply envelope (READ FIRST — this trips every new Cortex integration)

**Every Cortex XDR `public_api/v1` endpoint is POST with a uniform envelope:**
- Request body: `{"request_data": { ... actual filters / args here ... }}`
- Response body: `{"reply": { ... actual data here ... }}`

If you call any `public_api/v1` endpoint **without** wrapping the body in `request_data`, real Cortex returns **400**. Always wrap. Always unwrap.

GET requests (e.g., `/health`) are exempt — they have no body envelope.

## Endpoint Catalog (retrieval-only)

### Incidents

| Path | Body | Use |
|------|------|-----|
| `POST /public_api/v1/incidents/get_incidents` | `{"request_data":{"filters":[...],"search_from":0,"search_to":25,"sort":{"field":"creation_time","keyword":"desc"}}}` | List incidents (with filters) |
| `POST /public_api/v1/incidents/get_incident_extra_data` | `{"request_data":{"incident_id":"<id>","alerts_limit":5}}` | Incident detail + alerts + artifacts. **`alerts_limit ≤ 5`**; never 25 or 50 (enterprise responses can hit 400+ KB) |

### Alerts

| Path | Body | Use |
|------|------|-----|
| `POST /public_api/v1/alerts/get_alerts_multi_events/` | `{"request_data":{"filters":[...],"search_from":0,"search_to":25}}` | List alerts (with events) |

### Endpoints (hosts)

| Path | Body | Use |
|------|------|-----|
| `POST /public_api/v1/endpoints/get_endpoints` | `{"request_data":{"filters":[...],"search_from":0,"search_to":25}}` | List endpoints |

### XQL (Cortex's query language)

| Path | Body | Use |
|------|------|-----|
| `POST /public_api/v1/xql/start_xql_query/` | `{"request_data":{"query":"<XQL>","tenants":["<tenant>"]}}` | Start async XQL query → returns `query_id` |
| `POST /public_api/v1/xql/get_query_results/` | `{"request_data":{"query_id":"<id>"}}` | Poll for results |

## Filter Syntax

Cortex uses **structured filter objects**, not query strings. Each filter is an object with `field`, `operator`, and `value`:

```json
"filters": [
  { "field": "severity", "operator": "in", "value": ["critical","high"] },
  { "field": "creation_time", "operator": "gte", "value": <epoch_millis> },
  { "field": "hosts", "operator": "in", "value": ["web01"] },
  { "field": "status", "operator": "in", "value": ["new","under_investigation"] }
]
```

### Common fields
- `incident_id_list` — array of incident IDs
- `severity` — values: `low`, `medium`, `high`, `critical`
- `status` — values: `new`, `under_investigation`, `resolved_threat_handled`, `resolved_known_issue`, etc.
- `creation_time` / `modification_time` — epoch milliseconds
- `hosts` — hostname array
- `users` — username array
- `description_contains` — substring on incident description

### Operators
- `eq`, `neq` — equality
- `in`, `not_in` — set membership
- `gte`, `lte` — comparison (numeric / timestamp)
- `contains` — substring

### Time
**Cortex timestamps are epoch milliseconds**, not ISO 8601. Convert before filtering. "Last 24 hours" = `gte (now() - 24*3600*1000)`. The agent must compute the numeric value before issuing the request.

### Sorting / pagination
- `sort: {"field": "creation_time", "keyword": "desc"}` — desc/asc
- `search_from`, `search_to` — pagination indices (0-based, half-open)

## Common Ad-hoc Query Patterns (worked examples)

### 1. "Show me critical incidents"
```http
POST /public_api/v1/incidents/get_incidents
{"request_data":{
  "filters":[
    {"field":"severity","operator":"in","value":["critical","high"]},
    {"field":"status","operator":"in","value":["new","under_investigation"]}
  ],
  "fields":["incident_id","incident_name","severity","hosts","users","creation_time","alert_count","status"],
  "search_from":0,
  "search_to":25,
  "sort":{"field":"creation_time","keyword":"desc"}
}}
```
Render: table of (creation_time, severity, incident_id, incident_name, host_count, alert_count, status). The `fields` whitelist drops nested arrays (`file_artifacts`, `network_artifacts`) that bloat each row 5–10× on enterprise tenants. The `status` filter drops already-resolved noise. Reduce to `["critical"]` only if the user explicitly asks for critical-only.

### 2. "Incidents from the last 24 hours"
Compute `ts = (epoch_ms_now) - 86_400_000` first, then:
```http
POST /public_api/v1/incidents/get_incidents
{"request_data":{"filters":[{"field":"creation_time","operator":"gte","value":<ts>}],"sort":{"field":"creation_time","keyword":"desc"}}}
```

### 3. "Tell me about incident 56"
```http
POST /public_api/v1/incidents/get_incident_extra_data
{"request_data":{"incident_id":"56","alerts_limit":5}}
```
Render: incident BLUF (incident_name, severity, hosts, users, assigned_user, status, alert_count) → alerts table (severity, name, source, event_count) → artifacts summary. **Do not narrate phase-by-phase.** Just present the data.

### 4. "Incidents on host web01"
```http
POST /public_api/v1/incidents/get_incidents
{"request_data":{"filters":[{"field":"hosts","operator":"in","value":["web01"]}]}}
```

### 5. "Open incidents only"
```http
POST /public_api/v1/incidents/get_incidents
{"request_data":{"filters":[{"field":"status","operator":"in","value":["new","under_investigation"]}],"sort":{"field":"severity","keyword":"desc"}}}
```

### 6. "List endpoints"
```http
POST /public_api/v1/endpoints/get_endpoints
{"request_data":{"fields":["endpoint_id","endpoint_name","os_type","endpoint_status","isolate_status","last_seen","ip"],"search_from":0,"search_to":25}}
```
Render: table of (endpoint_id, endpoint_name, os_type, endpoint_status, isolate_status, last_seen).

### 7. "Find endpoint by hostname web01"
```http
POST /public_api/v1/endpoints/get_endpoints
{"request_data":{"filters":[{"field":"hostname","operator":"in","value":["web01"]}]}}
```

### 8. "Show alerts for incident 56"
```http
POST /public_api/v1/alerts/get_alerts_multi_events/
{"request_data":{"filters":[{"field":"incident_id_list","operator":"in","value":["56"]}]}}
```

### 9. XQL query (advanced retrieval)
Two-step async:
```http
# 1) start
POST /public_api/v1/xql/start_xql_query/
{"request_data":{"query":"dataset = xdr_data | filter event_type=PROCESS | limit 25"}}
# returns {"reply":{"query_id":"<id>"}}

# 2) poll
POST /public_api/v1/xql/get_query_results/
{"request_data":{"query_id":"<id>"}}
# returns {"reply":{"status":"SUCCESS","results":{"data":[...]}}}
```

## Response Field Reference

### Incident (`get_incidents` → `incidents[]`)
| Field | Use |
|-------|-----|
| `incident_id` | Identity |
| `incident_name` | Title |
| `severity` | low/medium/high/critical |
| `status` | new / under_investigation / resolved_* |
| `creation_time`, `modification_time` | Epoch ms |
| `hosts`, `users` | Affected entities |
| `host_count`, `user_count`, `alert_count` | Counts |
| `assigned_user_mail`, `assigned_user_pretty_name` | Owner |
| `description` | Free-text |
| `xdr_url` | Direct link to Cortex console |

### Incident extra data (`get_incident_extra_data` → `incident`)
Same as above PLUS:
| Field | Use |
|-------|-----|
| `alerts` | Embedded alert array |
| `network_artifacts`, `file_artifacts` | IOC tables |
| `incident_sources`, `original_tags` | Provenance |
| `mitre_tactics_ids_and_names`, `mitre_techniques_ids_and_names` | MITRE mapping (may be null) |

### Endpoint (`get_endpoints` → `endpoints[]`)
| Field | Use |
|-------|-----|
| `endpoint_id` | Identity |
| `endpoint_name` / `hostname` | Hostname |
| `os_type`, `os_version` | OS |
| `endpoint_status` | ONLINE / OFFLINE |
| `isolate_status` | NOT_ISOLATED / ISOLATED |
| `last_seen` | Epoch ms |
| `ip` | Endpoint IP |
| `users` | Logged-in users |

### Alert (`get_alerts_multi_events` → `alerts[]`)
| Field | Use |
|-------|-----|
| `alert_id` | Identity |
| `severity` | low/medium/high/critical |
| `name`, `category`, `description` | What/why |
| `source` | Detection source |
| `event_count` | Event-correlation depth |
| `host_ip`, `host_name`, `user_name` | Host attribution |
| `mitre_tactic_id_and_name`, `mitre_technique_id_and_name` | MITRE |

## Rendering Pattern

Tables, not JSON. Default columns:
- **Incidents:** creation_time · severity · incident_id · incident_name · host_count · alert_count · status · assigned_user_pretty_name
- **Alerts:** severity · name · source · host_name · event_count
- **Endpoints:** endpoint_id · endpoint_name · os_type · endpoint_status · isolate_status · last_seen · ip

≤5 rows → inline. >5 → top 10 + footer count. >50 → summary stats first.

For incident extra-data renders, structure as:
```
**Incident <id> — <name>** (severity / status / assigned)
  Hosts: <count>   Users: <count>   Alerts: <count>   xdr_url

  Alerts (top N by severity)
  <table>

  File artifacts (top N)
  <table>

  Network artifacts (top N)
  <table>
```

## Error handling

- **Empty `list_connectors`** — also walk all `mcp__crogld__*` MCP tools and match by vendor substring before declaring the role unfilled.
- **401 `Public API request unauthorized`** — connector secret invalid/expired. **Do NOT retry**. Surface the error, tell the user to re-issue the API key in Cortex Settings → API Keys, and stop.
- **400 with `request_data` in the error message** — you forgot the envelope. Re-issue with body wrapped in `{"request_data": {...}}`.
- **404 on `/public_api/v1/...`** — connector base_url is wrong (pointing at a non-Cortex host) OR the connector isn't a Cortex connector. Reroute.
- **200 `reply.status: PENDING` on XQL** — query still running; poll again with a short delay.
- **Time filter returning zero rows** — check the unit. Cortex timestamps are **epoch milliseconds**, not seconds, not ISO. Off-by-1000 silently returns nothing.

## Guardrails

- **Retrieval only.** This skill does NOT call `/public_api/v1/endpoints/isolate`, `/public_api/v1/incidents/update_incident/`, or any write endpoint. If the user asks to isolate / update / quarantine, tell them this is a retrieval-only skill and a separate response-action skill is needed.
- **No automated verdicts.** Don't write CONFIRMED / LIKELY / NOT-A-CAMPAIGN. Don't assign per-tenant priority scores. Don't compose attribution narratives. Render the data the user asked for; let them decide.
- **No `create_investigation` write-back.** Audit-write-back is a workflow-skill concept; this skill doesn't do that.
- **Cross-tenant sweeps only on explicit request.** If multiple Cortex connectors are registered and the user's question doesn't name a tenant, ask which one. Don't iterate silently.
- **Render before drilling.** Answer the question asked. Don't pre-emptively pull extra_data for every incident in a list — that blows context and isn't what was requested.
- **Time units.** Cortex = epoch milliseconds. The agent must compute the numeric value, not pass ISO strings to filters.
