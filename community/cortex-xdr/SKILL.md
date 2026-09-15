---
name: cortex-xdr
description: Drive a Palo Alto Cortex XDR connector via the `cortex_xdr.py` CLI. Read-only queries across alerts, incidents, and endpoints, with one extra-data hydration call for incidents. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Palo Alto Cortex XDR

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

Connector manifest: [connector.json](./connector.json).

One CLI: [scripts/cortex_xdr.py](./scripts/cortex_xdr.py). Run `scripts/cortex_xdr.py --help` for the subcommand list; `scripts/cortex_xdr.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. This connector is **read-only** -- there are no write or response actions exposed here.

**None of this connector's claims have been live-verified against a real Cortex XDR tenant.** It was built from Palo Alto Networks' published API and provisioning documentation only. Treat all behavior notes below as docs-grounded rather than tenant-proven.

## Scope

This connector implements a deliberately narrow, coherent read-only surface:

- **Alerts** via `POST /public_api/v1/alerts/get_alerts`
- **Incidents** via `POST /public_api/v1/incidents/get_incidents`
- **Incident extra data** via `POST /public_api/v1/incidents/get_incident_extra_data`
- **Endpoints** via `POST /public_api/v1/endpoints/get_endpoints` and `POST /public_api/v1/endpoints/get_endpoint`

The vendor docs also describe other Cortex XDR API families (response actions, installation packages, etc.), but they are out of scope for this connector.

## Auth recap

The connector stores three fields -- `api_host`, `key_id`, `api_key` -- and crogld attaches `x-xdr-auth-id: <key_id>` plus `Authorization: <api_key>` server-side on every call. Never ask the user for these values during analysis, never print them, and never construct the headers manually.

This connector supports **Standard** Cortex XDR API keys only. **Advanced** keys require a per-request `SHA256(api_key + nonce + timestamp)` authorization hash plus `x-xdr-nonce` / `x-xdr-timestamp` headers, which Crogl's static credential binding cannot generate.

## Tool call shape

```bash
cortex_xdr.py query-alerts --filter severity in high,critical --limit 25
cortex_xdr.py query-alerts --filter creation_time gte 1720000000000 --limit 10
cortex_xdr.py query-incidents --filter status eq under_investigation --limit 25
cortex_xdr.py query-incidents --filter description contains phishing --limit 10
cortex_xdr.py get-incident-extra-data --id 123456 --alerts-limit 100
cortex_xdr.py list-endpoints --limit 25
cortex_xdr.py query-endpoints --filter hostname eq workstation-42 --limit 10
cortex_xdr.py query-endpoints --filter last_seen gte 1720000000000 --sort-field last_seen --sort-dir DESC --limit 25
cortex_xdr.py view-result --ref <ref> --row 1
cortex_xdr.py grep --pattern severity
cortex_xdr.py ls --path /
```

`view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `cortex_xdr.py query-alerts` (`--filter FIELD OP VALUE`, `--offset`, `--limit`, `--sort-field`, `--sort-dir`, `--output`) | Query alerts from `POST /public_api/v1/alerts/get_alerts`. |
| `cortex_xdr.py query-incidents` (`--filter FIELD OP VALUE`, `--offset`, `--limit`, `--sort-field`, `--sort-dir`, `--output`) | Query incidents from `POST /public_api/v1/incidents/get_incidents`. |
| `cortex_xdr.py get-incident-extra-data --id <incident_id> [--alerts-limit N]` | Hydrate one incident's related alerts and artifacts from `POST /public_api/v1/incidents/get_incident_extra_data`. `--alerts-limit` is code-enforced as `click.IntRange(1, 100)`, defaulting to 100 -- the vendor's own default is 1000 with no documented maximum, but this connector holds it to the same hard cap as every other list/query command. |
| `cortex_xdr.py list-endpoints [--limit N] [--output FILE]` | Fetch the unfiltered endpoint inventory from `POST /public_api/v1/endpoints/get_endpoints`. This endpoint has no documented request fields -- re-verified 2026-09-10 against a public open-source client implementation: it genuinely accepts no filters, search window, or limit of any kind. The connector sends `{}` and truncates client-side to `--limit` after the full inventory is fetched; a real large tenant has been publicly reported returning ~7,500 rows from this call. Prefer `query-endpoints` (which does support `search_from`/`search_to` and filters) for anything beyond a quick check on a tenant with a large device fleet. |
| `cortex_xdr.py query-endpoints` (`--filter FIELD OP VALUE`, `--offset`, `--limit`, `--sort-field`, `--sort-dir`, `--output`) | Query filtered endpoints from `POST /public_api/v1/endpoints/get_endpoint`. |
| `cortex_xdr.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query/list call (full JSON). Pure cache read. |
| `cortex_xdr.py grep` | String-search this connector's knowledge-graph catalog. Reads persisted KG nodes, not the live API. |
| `cortex_xdr.py refresh` | Walk the catalog (`alerts`, `incidents`, `endpoints` -> field) and commit it to the KG from one sampled row per resource. |
| `cortex_xdr.py ls --path /<abs-path> --format json` | List this connector's schema by UNIX path. |
| `cortex_xdr.py sample` / `lookup` / `upload-samples` | KG edge-learning row fetch and provenance-anchored sample upload. |

## Query/filter syntax

### Common `--filter FIELD OP VALUE` shape

`query-alerts`, `query-incidents`, and `query-endpoints` all accept repeatable `--filter FIELD OP VALUE` triplets.

- `FIELD` must be one of the documented field names for that resource.
- `OP` must be one of the documented operators for that resource.
- `VALUE` is parsed as JSON if it looks like JSON, as an integer for plain digit strings, or as a plain string otherwise.
- For `in`, a comma-separated value like `high,critical` becomes a list automatically.

Multiple `--filter` flags are combined as **AND** because the vendor docs explicitly state OR is not supported.

### Alerts (`query-alerts`)

Documented filter fields:

- `alert_id_list`
- `alert_source`
- `severity`
- `creation_time`
- `server_creation_time`

Documented operators:

- `in`
- `gte`
- `lte`

Documented severity values include `low`, `medium`, `high`, `critical`; the 5.x docs also describe `informational` on the multi-events v2 endpoint, but this connector sticks to the v1 `get_alerts` query surface above.

Sorting:

- `--sort-field creation_time|severity`
- `--sort-dir asc|desc`

### Incidents (`query-incidents`)

Documented filter fields:

- `modification_time`
- `creation_time`
- `incident_id`
- `incident_id_list`
- `description`
- `alert_sources`
- `status`
- `starred`

Documented operators:

- `in`
- `contains`
- `gte`
- `lte`
- `eq`
- `neq`

Documented `status eq` values are:

- `new`
- `under_investigation`
- `resolved`

The vendor docs mention severity in prose for incidents, but the incident filter schema reviewed during this build does **not** document `severity` as a filter field, so this connector does not expose it as one.

Sorting:

- `--sort-field creation_time|incident_id|modification_time`
- `--sort-dir asc|desc`

### Endpoints (`query-endpoints`)

Documented filter fields:

- `endpoint_id_list`
- `endpoint_status`
- `dist_name`
- `first_seen`
- `last_seen`
- `ip_list`
- `group_name`
- `platform`
- `alias`
- `isolate`
- `hostname`
- `public_ip_list`
- `cloud_provider`
- `cloud_region`
- `cloud_provider_account_id`
- `cloud_instance_id`
- `cloud_id`

Documented operators:

- `in`
- `gte`
- `lte`
- `eq`

The same vendor page mentions `username` and `scan_status` in prose, but the reviewed `filters[].field` enum does **not** list them. This connector only accepts the documented enum values above.

Sorting:

- `--sort-field endpoint_id|first_seen|last_seen`
- `--sort-dir ASC|DESC`

## Context budget rules

- Default `--limit` is 25. Only raise it on explicit user request, and prefer narrowing filters first.
- Alerts and filtered endpoints have a documented max result set size of **100**. This connector enforces a hard cap of 100 rows on all list/query commands, including `get-incident-extra-data --alerts-limit` (previously an unbounded default of 1000 that contradicted this claim -- fixed 2026-09-10).
- `list-endpoints` has no server-side limiting param available at all (see Subcommands) -- its hard cap only bounds what's kept *after* the full inventory is fetched, not the request itself. Prefer `query-endpoints` for a large device fleet.
- Render the main fields per Rendering pattern below; don't paste full raw JSON for more than a couple of rows inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical query already in context -- use `view-result` on the existing `ref` instead.

## Resources

| Resource | Get by id? | In `refresh`'s KG walk? |
|---|---|---|
| `alerts` | No dedicated get; use `query-alerts --filter alert_id_list in ...` | Yes |
| `incidents` | No dedicated get; use `query-incidents --filter incident_id eq ...` | Yes |
| `endpoints` | No dedicated get; use `query-endpoints --filter endpoint_id_list in ...` | Yes |
| `incident-extra-data` | Yes (`get-incident-extra-data`) | No -- detail-only path with a strict 10 requests/minute limit |

## Uploading samples

Same provenance-anchored contract as other connectors in this repo: `upload-samples` requires either `--ref UUID` (from a prior query/list call's cache) or `--from-file FILE` (from that call's `--output`), plus the **exact same internal query description** the originating command used, and a `--catalog-path` under one of the walkable resources above (e.g. `/alerts/<field>`). Upload cap is 10 rows -- tighten `--limit` or filters before uploading rather than sampling in bulk.

## Edge-connectable fields

Useful scalar identity fields for cross-connector joins include:

- Alerts: `alert_id`, `external_id`, `endpoint_id`, `host_name`
- Incidents: `incident_id`, `incident_name`
- Endpoints: `endpoint_id`, `agent_id`, `host_name`, `ip`, `public_ip`

Do not edge against free-prose fields like incident descriptions or alert descriptions -- the inner-join requires whole-leaf equality.

## Rendering pattern

- **Alerts** -- `alert_id`, `severity`, `matching_status`, `name`, `category`, `source`, `host_name`, `detection_timestamp`
- **Incidents** -- `incident_id`, `incident_name`, `status`, `severity`, `creation_time`, `modification_time`, `alert_count`
- **Endpoints** -- `endpoint_id`, `host_name`, `endpoint_status`/`agent_status`, `platform`, `last_seen`, `ip`
- **Incident extra data** -- summarize counts and key related artifacts first, then use `view-result` or the returned JSON only when a deeper drill-down is actually needed

≤5 rows -> inline table. More than that -> show the first several plus a count of remaining matches, and offer to narrow the filters.

## Error handling

- **401/403** -- the connector's stored credential was rejected, has the wrong key type, or lacks the role/scope needed for the API call. Surface this as a configuration issue -- do not ask the user to paste a key into chat.
- **429 on `get-incident-extra-data`** -- the vendor docs document a limit of **10 requests per minute** for this endpoint. Wait 60 seconds before retrying.
- **429 elsewhere** -- tenant-wide throttling; the vendor docs also call out a **10 requests/second per tenant** API rate limit.
- **Zero-row alert/incident/endpoint responses** -- valid empty responses, not failures. Report zero rows plainly rather than retrying identically.
- **400** -- most likely a malformed filter field/operator/value combination. Re-check the documented enums above rather than guessing another field name.
- **5xx** -- upstream Cortex service issue; report the status and retry only if the user asks.

## Guardrails

- No write or response actions exist on this connector -- it is read-only by design.
- Do not imply endpoint isolation, script execution, incident status changes, or alert disposition changes are available through this connector.
- Do not expose or reconstruct `key_id` or `api_key`.
- Do not treat undocumented field values (for example incident severity filter values) as confirmed facts.

## Connector base URL

Configure the connector's base URL as `https://<api_host>`, where `api_host` is the tenant-specific Cortex XDR API FQDN shown on the API Keys page (for example `api-company.us.paloaltonetworks.com`). The CLI sends full endpoint paths such as `/public_api/v1/incidents/get_incidents` per call.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- the skill auto-resolves its bound connector; the server applies the `x-xdr-auth-id` / `Authorization` headers from the stored connector credential.

## Output format

Query/list commands return grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). `get-incident-extra-data`, `view-result`, and KG commands return JSON.
