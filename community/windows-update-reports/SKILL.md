---
name: windows-update-reports
description: Drive a Windows Update for Business reports connector via the `windows_update_reports.py` CLI. Run KQL queries over the 8 WUfB reports tables (device compliance, update status, readiness, alerts, Delivery Optimization) in a customer's Log Analytics workspace, drill into a result, walk the catalog into the knowledge graph, and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Windows Update for Business reports

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/windows_update_reports.py`. Run `scripts/windows_update_reports.py --help` for the subcommand list; `scripts/windows_update_reports.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. This connector is read-only -- there are no write/remediation actions (there is no meaningful write API for WUfB reports data in the first place; even *enrolling* a tenant into the service is a manual Azure-portal-only step, not something any API call, including this connector's, can do).

**This is not a REST resource connector.** Unlike Intune or Defender in this repo, there is no `$filter`-able resource list. The entire surface is one Azure Monitor Log Analytics query endpoint (`POST .../query` with a KQL body) against a Log Analytics workspace the customer owns. `query` takes a full KQL pipeline, not a `--resource`/`--filter` pair, and there is no separate `get`-by-id command -- a KQL `where` clause covers point lookups the same way `query` covers search.

## Auth recap

The connector stores four fields -- `tenant_id`, `client_id`, `client_secret`, `workspace_id` -- and crogld exchanges the first three server-side for a bearer token via the tenant's `https://login.microsoftonline.com/<tenant_id>/oauth2/v2.0/token` endpoint (client-credentials grant, `https://api.loganalytics.io/.default` scope), then attaches the `Authorization: Bearer` header automatically on every call to `https://api.loganalytics.azure.com/v1/workspaces/<workspace_id>/query`. Never ask the user for these values during analysis, never print them, and never construct auth headers manually.

**This connector's permission model is genuinely different from Intune/Defender in this repo.** There is no Graph API permission or admin-consent step involved at all. Access is gated purely by an **Azure RBAC role** (`Log Analytics Reader`) assigned to the app registration's service principal on the specific Log Analytics workspace resource, via that workspace's Access control (IAM) blade. If queries start failing with an auth-shaped error, the fix lives in Azure IAM on the workspace, not in Entra API permissions.

## Tool call shape

```bash
windows_update_reports.py query --query "UCClient | where OSVersion contains 'Windows 11'" --limit 25
windows_update_reports.py query --query "UCClientUpdateStatus | where ClientSubstate == 'RestartRequired'" --limit 25
windows_update_reports.py query --query "UCUpdateAlert | where AlertStatus == 'Active'" --limit 25
windows_update_reports.py view-result --ref <ref> --row 1
windows_update_reports.py grep --pattern ReadinessStatus
windows_update_reports.py ls --path /
```

`--limit` is **required** on `query` -- there is no default, and the CLI errors immediately if it's omitted. It's appended to the query as a trailing `| take <limit>`, which is how this API bounds row count (there's no OData-style `@odata.nextLink` to page through). It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped. `view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `windows_update_reports.py query` (`--query`, **`--limit` required, hard cap 100**, `--output`) | Run a KQL query against one of this connector's 8 tables. Emits grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `windows_update_reports.py grep` | String-search this connector's knowledge-graph catalog — matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes (populated by `refresh`), not the live API. |
| `windows_update_reports.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full record JSON). Pure cache read — rows are returned in full at query time. |
| `windows_update_reports.py upload-samples` | Upload sample rows to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (a `--output` artifact), plus the exact `--query` value used to fetch them, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or when the read-before-write check fails. |
| `windows_update_reports.py refresh` | Walk every configured connector's catalog (table → field) and commit each tier to the KG via `commit_walk_tier`. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. Run after configuring a new connector. |
| `windows_update_reports.py ls --path /<abs-path> --format json` | List this skill's connectors' database schema by UNIX path (Table → Field). Field names are discovered from the query API's own column metadata on a one-row sample, not JSON-key sniffing. |

## Context budget rules

- Default to a small `--limit` (25 is a reasonable starting point for an analyst-facing question). Only raise it on explicit user request, and prefer narrowing the KQL `where` clause first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, so there's no "just set --limit very high" option for bulk population; run multiple narrower `query`s instead.
- An unfiltered `<table> | take N` still returns a broader unfiltered slice for knowledge-graph population, but it's bounded by the same 100-row hard cap as everything else -- for an analyst-facing question, filter on a device id, update category, or alert status instead of fetching broadly and filtering client-side.
- Render the main fields per Rendering pattern below; do not paste full raw JSON for more than a couple of rows inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## KQL query syntax

`--query` is a full KQL pipeline that **must start with one of this connector's 8 table names** (enforced client-side before the request is ever sent -- see Guardrails). String equality is `==`, not OData's `eq`; `contains` works the same way it does in Intune/Defender's OData filters. Quote the whole expression in the shell.

```text
UCClient | where AzureADDeviceId == "01234567-89ab-cdef-0123-456789abcdef"
UCClient | where OSVersion contains "Windows 11"
UCClientUpdateStatus | where UpdateCategory == "WindowsQualityUpdate" and UpdateReleaseTime == datetime(2023-03-14)
UCClientUpdateStatus | where ClientSubstate == "RestartRequired"
UCUpdateAlert | where ErrorCode == "0x8024000b"
UCUpdateAlert | where AlertStatus == "Active" | summarize Devices=count() by AlertSubtype
UCClient | summarize count() by TimeGenerated
```

The last one is the health-check query Microsoft's own docs recommend to confirm data is actually flowing into the workspace at all -- reach for it whenever a query on a plausible filter comes back empty and it isn't obvious whether that's a real zero-match result or the workspace has no WUfB data yet (see Error handling).

## Resources (tables)

All 8 WUfB reports tables (Microsoft Learn: `wufb-reports-schema`) are in scope:

| Table | Category | Use it for |
|---|---|---|
| `UCClient` | Device record | One row per device -- current build, OS edition, and per-update-type compliance status fields (`OSFeatureUpdateComplianceStatus`, `OSQualityUpdateComplianceStatus`, `OSSecurityUpdateComplianceStatus`). The overall "is this device compliant" table. |
| `UCClientUpdateStatus` | Device record | One row per (device, applicable update) combining client- and service-side state -- `ClientSubstate`, `TargetBuild`/`TargetKBNumber`, install/restart timestamps. Multiple rows per device are normal (see Error handling). |
| `UCClientReadinessStatus` | Device record | One row per device describing Windows 11 hardware-readiness (`ReadinessStatus`, `ReadinessReason` -- e.g. `CPU;TPM` when blocked) and separately Windows-setup readiness (`SetupReadinessStatus`/`SetupReadinessReason`). |
| `UCUpdateAlert` | Service and device record | Alerts needing attention for one device/update/deployment -- `AlertStatus`, `AlertSubtype`, `ErrorCode`/`ErrorSymName`, `Recommendation`. Certain fields are blank depending on `AlertType` (client vs. service alert). |
| `UCDeviceAlert` | Service and device record | Device-specific alerts *not* tied to one particular update (e.g. an end-of-service alert) -- distinct from `UCUpdateAlert`, which is always relative to one update. |
| `UCServiceUpdateStatus` | Service record | Service-side-only view of one (device, update, deployment) -- `ServiceState`/`ServiceSubstate`, deployment/policy timestamps. A near-real-time subset of what `UCClientUpdateStatus` eventually shows once the client side reports in. |
| `UCDOStatus` | Device record | Per-device Delivery Optimization / Microsoft Connected Cache bandwidth usage (bytes from cache/CDN/peers, peering status) by content type. |
| `UCDOAggregatedStatus` | Device record (tenant aggregate) | Tenant-wide rollup of `UCDOStatus`, summarized by `ContentType` with a `DeviceCount`. |

Per Microsoft's own FAQ: use `UCClient` for overall device compliance, `UCClientUpdateStatus` for per-deployment update status, and `UCUpdateAlert` to understand and act on update failures.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded row must be the byte-identical output of a recent `query` issued from this same container. The round-trip (query → see results → choose to upload) is what forces the LLM to be judicious about what is sampled. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** — capture the `Result reference: <uuid>` line that `query` prints on stdout and pass that UUID. The container-local cache holds the rows that query just returned.
2. **`--from-file FILE`** — when the prior `query` was invoked with `--output FILE`, pass that file. Its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

In either form the **exact same `--query` value** must be passed to `upload-samples` as was passed to the originating `query`; a mismatched value fails the read-before-write check.

The upload cap is **10 rows** (`MAX_UPLOAD_ROWS=10`). Tighter filters are the expected pattern — `--limit 1` is what to reach for first when sampling a single table. The point is one or a handful of exemplary rows per leaf, not bulk data.

`--catalog-path` (required) is the inventory path under which the sample is recorded — typically `/<table>/<field>` (e.g. `/UCClient/OSFeatureUpdateComplianceStatus`). The first tier is the table name. Tiers are created lazily server-side, and the server walks each sample's nested JSON keys into child nodes under the leaf.

## Edge-connectable fields

When picking source or target nodes for `kg_learner.py create-edge`, use **scalar identity fields** from a sampled row. The server's inner-join check intersects bare JSON string leaves between two `data_samples`; WUfB rows expose several:

- Device: `AzureADDeviceId` (every table carries this), `DeviceName`, `GlobalDeviceId`, `SCCMClientId`
- Tenant: `AzureADTenantId` / `TenantId`
- **Cross-connector join target**: `AzureADDeviceId` is the same Entra device identifier Intune's `managed-devices` exposes as `azureADDeviceId` and Defender's `machines` exposes as `aadDeviceId` -- this is the natural edge to a device's Intune compliance record or Defender machine record from this connector's rows.

**Do not edge against free prose** like `Description` or `Recommendation` narrative fields on `UCUpdateAlert`/`UCDeviceAlert` -- the inner-join requires whole-leaf equality, so an identifier embedded in prose won't match a bare leaf from another connector.

## Rendering pattern

Render a compact card or table per device/row -- don't dump raw PSV/JSON as the answer:

- **Device compliance (`UCClient`)** -- `DeviceName` · `OSVersion`/`OSBuild` · `OSFeatureUpdateComplianceStatus` · `OSQualityUpdateComplianceStatus` · `OSSecurityUpdateComplianceStatus` · `LastWUScanTime [UTC]`.
- **Update status (`UCClientUpdateStatus`)** -- `DeviceName` · `TargetKBNumber`/`TargetBuild` · `UpdateCategory` · `ClientSubstate` · `ClientSubstateTime [UTC]`.
- **Readiness (`UCClientReadinessStatus`)** -- `DeviceName` · `ReadinessStatus` · `ReadinessReason` · `TargetOSVersion`.
- **Alerts (`UCUpdateAlert`/`UCDeviceAlert`)** -- `DeviceName` · `AlertStatus` · `AlertSubtype` · `ErrorCode`/`ErrorSymName` · `Recommendation`.

≤5 rows → inline table. More than that → show the first several plus a count of remaining matches, and offer to narrow the query.

## Error handling

- **This connector's `query` rejects a KQL pipeline whose leading table isn't one of the 8 above, before ever calling the API.** This is intentional, not a bug -- a shared Log Analytics workspace often carries other data sources (Sentinel, other Azure diagnostics), and this connector is deliberately scoped to WUfB's own tables only. Drop the unsupported table reference rather than retrying the same query.
- **401/403** -- unlike Intune/Defender in this repo, this is very unlikely to mean a bad Graph permission grant. It means either the bearer token itself is invalid/expired (should self-heal via the connector's OAuth flow) or, far more likely for this connector, **the app registration's service principal has not been assigned the `Log Analytics Reader` role on this specific workspace** (see Auth recap) -- an Azure IAM gap, not a credential-value gap. Surface this as a provisioning issue for whoever set up the connector; do not ask the user to paste a token.
- **400** -- a KQL syntax error (unbalanced quotes, wrong equality operator, unknown column). Don't retry the identical query; fix the syntax per the query section above, or run `grep`/`ls` to confirm the real column name.
- **Zero rows on a 200** -- a real, valid empty result, but there are several genuinely different reasons for it here, so don't assume the query itself is wrong:
    - **No WUfB data in this workspace at all yet** -- run the health-check query `UCClient | summarize count() by TimeGenerated`; if that's also empty, the issue is upstream of this query (enrollment not complete, or within its propagation window -- see below), not the filter.
    - **A specific device/table has no rows** -- the device may not be Entra-joined (workplace-registered-only devices are explicitly unsupported), may not have been active in the last 28 days, or may simply have no alerts/update activity to report (a real, healthy state for `UCUpdateAlert`).
    - **A genuinely narrow filter matched nothing** -- widen it before concluding there's a data problem.
- **Propagation delays operate on very different timescales -- don't apply one universal "wait and retry" to all of them.** Per Microsoft's own docs: 24-48 hours after initial enrollment/configuration before any data shows up at all; up to 14 days for *all* devices in the tenant to appear (inactive devices take longer); up to 21 days specifically for device *names* to populate even after the `AllowDeviceNameInDiagnosticData` policy is set (the device ID shows up well before the name does). If a query looks empty right after provisioning, check which of these windows is actually in play before concluding something is misconfigured.
- **429** -- the API is throttling; back off and report the rate limit rather than retrying in a loop.
- **5xx** -- upstream service issue; report the status and retry only if the user asks.

## Guardrails

- No write actions exist on this connector -- it is read-only. There is no meaningful write API for WUfB reports data to begin with; do not imply enrollment, policy changes, or remediation are possible through it.
- This connector's queries are scoped to its 8 declared tables only (enforced client-side, see Error handling) -- do not attempt to work around this to query other tables that might exist in the same shared Log Analytics workspace (e.g. Sentinel data); that data is out of this connector's intended scope even if the underlying credential could technically reach it.
- Do not treat `OSFeatureUpdateComplianceStatus`/`OSQualityUpdateComplianceStatus`/`OSSecurityUpdateComplianceStatus` alone as a final compliance verdict without checking `LastWUScanTime [UTC]` for staleness, the same caution Intune's `SKILL.md` gives for `complianceState` -- an old scan can mean the compliance status itself is stale.
- Several fields across these tables are explicitly documented by Microsoft as **"Currently, data isn't gathered to populate this field"** (e.g. `UCClient.WUAutomaticUpdates`, `UCDeviceAlert.ErrorCode`, `UCServiceUpdateStatus.DeploymentName`) -- a null/empty value there is expected per Microsoft's own schema docs, not evidence of a misconfigured connector or a query bug.
- Do not expose or reconstruct the tenant ID, client ID, client secret, or workspace ID.
- Do not fabricate device or alert data when a query returns zero results -- report zero results plainly, and use the health-check query in Error handling to determine which of the several real causes applies before suggesting a fix.

## Connector base URL

Configure the connector's base URL as this tenant's **Log Analytics query root, including the workspace ID**: `https://api.loganalytics.azure.com/v1/workspaces/<workspace_id>`. Unlike Intune/Defender (whose base URL is a fixed, product-wide API root), this URL is per-customer -- it must be provisioned with the specific workspace's GUID, not a generic Microsoft endpoint. The CLI sends `POST /query` with the KQL body; auth is applied server-side from the connector's stored secret.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets — the skill auto-resolves its bound connector; the server applies the OAuth Bearer stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
