---
name: tanium
description: Drive a Tanium Cloud connector via the `tanium.py` CLI. Read-only queries across four modules -- Asset (software/hardware inventory), Comply (compliance and CVE findings), Patch (patch definitions, deployments, applicability), and Threat Response (alerts). Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Tanium

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/tanium.py`. Run `scripts/tanium.py --help` for the subcommand list; `scripts/tanium.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. This connector is **read-only** -- there are no write/mutation actions (no Asset upsert, no Patch deployment create/stop, no Patch list upsert, no Threat Response alert resolve) available through it, by deliberate design, not by API limitation.

**This connector targets Tanium Cloud only.** On-prem Tanium's API surface differs (per Tanium's own docs) and has not been evaluated here.

**Live-verified against a real Tanium Cloud tenant (2026-07-22)**, via the actual crogld agent/MCP flow, not raw curl. Asset, Comply, and Patch (all Gateway GraphQL) each returned real rows for the tenant's one registered test endpoint -- confirming the shared auth token, host, and GraphQL call shape all work end-to-end. Threat Response (legacy Detect3 REST) returned a clean 0-row/"empty-result" response rather than an error; since the same shared auth token works for every Gateway-backed module, this is more likely a genuinely quiet tenant (no alerts have fired on the one test endpoint) than a stale endpoint path or auth/scope problem, but it remains formally unconfirmed -- see "Confidence levels by module" below. It was originally built entirely from vendor documentation (Tanium Gateway's help.tanium.com user guide, read live and authenticated) plus, for Threat Response only, a third-party open-source integration's source code.

**Added and live-verified 2026-07-23:** `query-asset-list`, a bulk Asset REST listing built after discovering that `query-asset-endpoints` (Gateway) cannot see endpoints written via Asset's Import API (`assetUpsertEndpoints`) -- confirmed by live testing against a tenant with 2,167 total Asset records (2,166 Import API-sourced, 1 real TDS-registered client). `query-asset-list` correctly returned the full population, correctly paginated past its 500-row per-call page size; `query-asset-endpoints` returned only the real client. The response also carries a `provider_type`/`provider_name` field pair that cleanly distinguishes Import API-sourced rows from real-client rows -- useful for any future filtering/rendering by data provenance. See "Three transports behind one connector" below.

## Three transports behind one connector

- **Asset (product/endpoint-scoped), Comply, and Patch** go through **Tanium Gateway**, a single GraphQL endpoint (`/plugin/products/gateway/graphql`) that is Tanium's current preferred integration point for most modules. Verified directly against Tanium's live Gateway User Guide. **Important gap, confirmed live 2026-07-23:** Gateway's `assetProductEndpoints` query (`query-asset-endpoints`) only resolves endpoints with a specific software product installed, from real TDS/sensor telemetry -- it cannot see endpoints written via Asset's Import API (`assetUpsertEndpoints`) at all, regardless of filter.
- **Asset** also has its own **bulk REST listing** (`query-asset-list`, `GET /plugin/products/asset/v1/assets`), a separate, documented public API that a third-party integration (Brinqa) already builds against. Cursor-paginated via `limit`/`minimumAssetId` request params and a `meta.{nextAssetId,endOfReader}` response envelope. **Confirmed live 2026-07-23 that this is the read path that surfaces Import API-sourced records** -- the connector's other two Asset transports (`query-asset-endpoints` above, and Gateway's plain `endpoints()` used internally by Comply/Patch) cannot. Same host, same `session` header auth as everything else.
- **Threat Response** has no query/list capability in Gateway at all -- the only Threat-Response operation Gateway documents is a `threatResponseAlertResolve` mutation (resolve-by-GUID), which is out of scope for this read-only connector. Listing/reading alerts instead goes through the **legacy Detect3 REST API** (`/plugin/products/detect3/api/v1/alerts`), reached over the same host and the same auth header as Gateway.

All three transports share one credential (host + API token) because Tanium's `/plugin/products/<service>/...` reverse-proxy convention puts every module's API, old and new, behind the same Cloud API host.

## Confidence levels by module

| Module | Transport | Confidence |
|---|---|---|
| Asset | Gateway GraphQL | **Live-verified 2026-07-22** -- `query-asset-endpoints` returned the tenant's one real registered endpoint. `query-asset-products` returned 0 rows (no software inventory catalogued yet for that endpoint -- not a permissions failure, endpoints came back, products didn't). Two mutations were marked Experimental/RC in the doc, but this connector doesn't use them (read-only). **Confirmed 2026-07-23 this transport cannot see Import API-sourced Asset records at all** -- use `query-asset-list` for those. |
| Asset (bulk listing) | REST (`/v1/assets`) | **Live-verified 2026-07-23** -- `query-asset-list` returned all 2,167 records in a real tenant: 2,166 Import API-sourced (`provider_type: "Asset: Import API"`, `provider_name` identifying the specific Import source, e.g. `evil-corp-import-api`/`evil-corp-import-api-v2`) plus the 1 real TDS-registered endpoint (`provider_type: "Tanium Client"`), correctly paginated via `minimumAssetId`/`endOfReader` past the 500-row per-call page size. `id`, `computer_name`, `provider_type`, `provider_name` are confirmed present on every row. Other fields (IP/MAC/OS) were not exercised by this test. Field names are not independently confirmed against an official Tanium doc (this connector's fetch of help.tanium.com's own REST reference page could not render its body content); shape is cross-checked against a live response and a third-party (Brinqa) integration's description instead. |
| Comply | Gateway GraphQL | **Live-verified 2026-07-22** -- `query-comply-cves` returned one real row for the tenant's endpoint (endpoint identity fields populated; CVE-specific fields empty, consistent with no Comply scan having produced CVE hits against it yet). |
| Patch | Gateway GraphQL | **Live-verified 2026-07-22** -- `query-patch-definitions` returned 3 real rows (Ubuntu `noble` definitions: `libsqlite3-0`, `wget`, `snapd`). The exact filter input shape for `patchDefinitions` is still assumed consistent with Comply's documented filter primitive, not independently confirmed -- the live test ran unfiltered. |
| Threat Response | Legacy Detect3 REST | **Live-tested 2026-07-22, still not confirmed.** `query-threat-response-alerts` returned a clean 0-row/"empty-result" response (not a 401/403/404) against the same tenant where every Gateway-backed module returned real data with the same auth token -- so this is more likely a genuinely quiet tenant (no alerts have fired against the one registered test endpoint) than an auth/scope problem, but a 0-row response can't distinguish that from a stale endpoint path. developer.tanium.com's own Threat-Response-alerts page still 404s, and Tanium's release notes state Threat Response 4.0 changed its API. The endpoint paths and `session`-header auth used here are cross-checked against Palo Alto's open-source Cortex XSOAR "TaniumThreatResponse" integration source, which may predate that 4.0 change. Treat this module's endpoint paths, response shape, pagination, and filter support as unconfirmed until an actual alert fires on a real endpoint and is retrieved through this path. As of 2026-09, `query-threat-response-alerts` also sends `limit`/`offset` query params (the same convention as Tanium's other legacy REST module APIs) so the request itself is bounded rather than fetched-then-truncated -- this specific param pair is likewise not independently confirmed for the Detect3 alerts endpoint; if a live alert-bearing tenant ever returns a 400 pointing at these params, that's the first thing to revisit. |

Asset, Comply, and Patch are now live-verified end-to-end (auth, host, and GraphQL call shape all confirmed against a real tenant). Threat Response still needs a real alert to fire on a registered endpoint before its transport can be considered confirmed rather than merely "didn't error." Spot-check the exact `patchDefinitions` filter shape and the Comply/Patch OR-toggle field name against Gateway's own Documentation Explorer (Tanium Console -> Shared Services -> Gateway -> Docs) when convenient -- neither was exercised by the live test above (which ran unfiltered).

## Auth recap

The connector stores two fields -- `api_host` and `api_token` -- and crogld attaches the token as the raw value of a `session` header on every request, to both Gateway and the legacy Threat Response REST path. Never ask the user for the host or token during analysis, never print them, and never construct the `session` header manually.

## Tool call shape

```bash
tanium.py query-asset-products --search "Chrome" --limit 25
tanium.py query-asset-endpoints --vendor "Google" --name "Chrome" --limit 25
tanium.py query-asset-list --limit 100
tanium.py query-comply-findings --computer-group "All Windows" --filter "category:Error" --limit 25
tanium.py query-comply-cves --cvss-version v3 --filter "cveYear:2024" --limit 25
tanium.py query-patch-definitions --filter "severity:Critical" --limit 25
tanium.py query-patch-applicability --computer-group "All Windows" --limit 25
tanium.py get-patch-deployment --id <deployment-id>
tanium.py query-threat-response-alerts --limit 25
tanium.py get-threat-response-alert --id <alert-guid>
tanium.py view-result --ref <ref> --row 1
tanium.py grep --pattern cve
tanium.py ls --path /
```

`--limit` defaults to 25, code-enforced hard cap 100 (`click.IntRange(1, 100)`), on every query command except `query-asset-list`, which defaults to 100 with a hard cap of 2000 (it's a bulk-listing command, and this connector's other Asset commands are already narrow/scoped). A `--limit` above the hard cap is rejected by the CLI itself, not silently clamped. `view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `query` (`--resource {comply-findings,comply-cves,patch-definitions}`, `--filter`, `--computer-group`, `--cvss-version`, `--limit`, `--output`) | Generic dispatcher over the three resources that share this connector's filter DSL. For Asset products/endpoints or Patch applicability (different flag shapes), use the dedicated commands below instead. |
| `query-asset-products` (`--vendor`, `--search`, `--state`, `--limit`, `--output`) | Tanium Asset products (`query.assetProducts`). |
| `query-asset-endpoints` (`--vendor`, `--name`, `--version`, `--limit`, `--output`) | Endpoints tied to an Asset product (`query.assetProductEndpoints`). Real TDS-registered clients only -- does not see Import API-sourced records. |
| `query-asset-list` (`--limit` default 100, hard cap 2000, `--output`) | Every Asset record, any source, via Asset's own bulk REST API (`GET /v1/assets`). The only transport that surfaces Import API-sourced (`assetUpsertEndpoints`) records. |
| `query-comply-findings` (`--computer-group`, `--filter`, `--limit`, `--output`) | Compliance/benchmark findings, one row per finding (`endpoints.compliance.complianceFindings`). |
| `query-comply-cves` (`--cvss-version`, `--computer-group`, `--filter`, `--limit`, `--output`) | CVE findings, one row per finding, field set forks by CVSS version (`endpoints.compliance.cveFindings`). |
| `query-patch-definitions` (`--filter`, `--limit`, `--output`) | Patch definitions (`query.patchDefinitions`). Requires the tenant's "Patch Show" permission. |
| `query-patch-applicability` (`--computer-group` **required**, `--limit`, `--output`) | Per-endpoint patch applicability via the fixed `Patch - Patch List Applicability` sensor. |
| `get-patch-deployment` (`--id` required) | Hydrate one Patch deployment (`query.patchDeployment`). Requires "Patch Deployment Read" permission. |
| `query-threat-response-alerts` (`--limit`, `--output`) | List Threat Response alerts. **Best-effort** -- see Confidence levels above. |
| `get-threat-response-alert` (`--id` required) | Hydrate one Threat Response alert by GUID. **Best-effort.** |
| `view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full entity JSON). Pure cache read. |
| `grep` | String-search this connector's KG catalog. Reads persisted KG nodes, not the live API. |
| `refresh` | Walk the catalog (resource -> field) and commit each tier to the KG. `patch-applicability` and `patch-deployments` are excluded (see Resources below). |
| `ls --path /<abs-path> --format json` | List this connector's schema by UNIX path. |
| `sample` / `lookup` | KG edge-learning row fetch, per resource. |
| `upload-samples` | Upload sample rows to the knowledge graph (provenance-anchored to a prior query). |

## Context budget rules

- Default `--limit` (25) is a reasonable starting point for an analyst-facing question. Only raise it on explicit user request, and prefer narrowing a filter or `--computer-group` first. `--limit` is code-enforced (`click.IntRange`) at 100 for every command except `query-asset-list` (2000) -- a request above the cap is rejected outright, not silently clamped, so raise a filter/`--computer-group` instead of assuming a bigger `--limit` will work.
- Comply findings and CVE findings are returned **one row per finding**, not one row per endpoint -- an endpoint with many findings can multiply row count well past `--limit`'s endpoint-level intent. The client-side truncation to `--limit` in this connector's code caps the final row count, but a single very-noisy endpoint can still consume most of a small limit; narrow with `--computer-group` or `--filter` first.
- Every Gateway query (`query-asset-products`, `query-asset-endpoints`, `query-comply-findings`, `query-comply-cves`, `query-patch-definitions`, `query-patch-applicability`) requests one page and does not follow Gateway's cursor -- if more rows exist upstream than `--limit`, the command prints a `Note: more ... are available upstream` line to stderr rather than silently returning a partial result that looks complete. Treat that note as a prompt to narrow the filter/`--computer-group`, not just raise `--limit` past the hard cap.
- Render the main fields per Rendering pattern below; don't paste full raw JSON for more than a couple of rows inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical query already in context -- use `view-result` on the existing `ref` instead.

## Query/filter syntax

### Asset products / endpoints

Dedicated flags, not the generic filter DSL below: `--vendor`, `--search`, `--state` (products) or `--vendor`/`--name`/`--version` (endpoints). Only one Asset product state (`Cataloged`) is confirmed in vendor docs; other state values are tenant/vendor-defined and untested here -- pass whatever the Tanium console shows, but don't assume a wrong guess is this connector's bug.

### Asset list

`query-asset-list` takes only `--limit`/`--output` -- no filter support (the underlying REST API is a flat cursor-paginated listing, not a query language). If the user wants a specific host, raise `--limit` and have the agent filter client-side on the returned rows, or note that a filtered version of this command isn't available yet.

### Comply and Patch: shared filter DSL

`--filter` takes comma-separated AND terms:

| Syntax | Meaning |
|---|---|
| `path:value` | Equality (Gateway's default when no operator is given). |
| `path!:value` | Negated equality (`negated: true`). |
| `path>=value` | Greater-than-or-equal (`op: GTE` -- the **only** comparison operator confirmed in Gateway's docs). |

Combine terms with a comma for AND: `"category:Error,firstFoundDate>=2025-07-10"`.

**Not supported, on purpose:** `<=`, `>`, `<`, and OR-combination. `GTE` is the only comparison-operator enum value shown in Gateway's docs; guessing the others' exact enum member names risked sending an invalid query. OR-combination is skipped because Tanium's own docs disagree with each other on the toggle field's name (`or` in the Gateway Overview page's general filter section vs. `any` in the Comply examples page) -- rather than guess, this connector's filters are always AND-combined. If a live tenant becomes available, check Gateway's Documentation Explorer for the real `FieldFilterOp` enum and the correct OR-toggle field name, and extend `_parse_gateway_filter` in `scripts/tanium.py` accordingly.

`--computer-group` (Comply findings/CVEs, Patch applicability) scopes to endpoints in a named computer group (`memberOf.name`); omit it to scan all endpoints up to `--limit` (not available for `query-patch-applicability`, where it's required -- see Guardrails).

Patch definitions' filter shape is **assumed**, not independently confirmed -- Gateway's docs name a `PatchDefinitionFieldFilter` input type without showing its literal field names, so this connector reuses Comply's confirmed `path`/`op`/`value`/`negated` shape on the assumption Gateway's filter primitive is shared infrastructure. If `query-patch-definitions --filter` errors with a schema-mismatch message, that assumption may be wrong.

### Comply CVE findings: CVSS version fork

`--cvss-version {v2,v3,v4}` (default `v3`) selects which field set to request -- the v2/v3/v4 field sets are **not co-selectable** in one query (Tanium's schema forks the whole shape per version, e.g. `cvssScoreV3`/`severityV3` for v3 vs. `cvssScoreV4`/`cwes`/`mitreAttacks`/EPSS fields for v4). Pick the version the user actually wants before querying rather than defaulting blindly.

## Resources

| Resource | Get by id? | In `refresh`'s KG walk? |
|---|---|---|
| `asset-products` | No | Yes |
| `asset-endpoints` | No | Yes |
| `asset-list` | No | Yes |
| `comply-findings` | No | Yes |
| `comply-cves` | No | Yes |
| `patch-definitions` | No | Yes |
| `patch-applicability` | No | **No** -- requires a customer-specific `--computer-group` up front, so it can't be sampled unscoped the way `refresh` needs. |
| `patch-deployments` | Yes (`get-patch-deployment`) | **No** -- Gateway only documents a get-by-id lookup for deployments, no list/filter capability, so there's nothing to walk. |
| `threat-response-alerts` | Yes (`get-threat-response-alert`) | Yes, but best-effort (see Confidence levels). |

## Uploading samples

Same provenance-anchored contract as other connectors in this repo: `upload-samples` requires either `--ref UUID` (from a prior `query-*` command's cache) or `--from-file FILE` (from that command's `--output`), plus the **exact same internal query description** the originating command used (visible via `--json-meta`'s `query` field), and a `--catalog-path` under one of the walkable resources above (e.g. `/comply-findings/<field>`). Upload cap is 10 rows -- tighten `--limit`/`--filter` before uploading rather than sampling in bulk.

## Edge-connectable fields

Scalar identity fields usable with `kg_learner.py create-edge`:

- Asset endpoints (`query-asset-endpoints`): `id`, `eid`, `computerName`, `computerId`, `serialNumber`, `ipAddress`, `userName`
- Asset list (`query-asset-list`): `id`, `computer_name` (hostname), `provider_type`, `provider_name` confirmed present on every row observed live (2,167-row test). `provider_type`/`provider_name` distinguish Import API-sourced records (`"Asset: Import API"` / the specific source name) from real TDS-registered clients (`"Tanium Client"`) -- useful for edging or filtering by data provenance. Other fields (IP, MAC, OS) vary by record and their exact key names are NOT independently confirmed against an official Tanium doc -- inspect a live sample via `view-result` before edging on anything beyond these four.
- Comply findings/CVEs: `endpointName`, `endpointIpAddress` (added by this connector to each finding row for join context), `cveId`
- Patch definitions: `id`, `cveIds`
- Threat Response alerts: whatever scalar identity fields the live response actually returns -- unconfirmed, verify against a real payload first.

Do not edge against free-prose fields (Comply's `cisaShortDescription`/`summary`, CVE `remediation` text, etc.) -- the inner-join requires whole-leaf equality.

## Rendering pattern

- **Asset products** -- vendor, name, installation counts, tracking state.
- **Asset endpoints** -- computerName, serialNumber, operatingSystem, ipAddress.
- **Asset list** -- `computer_name` (hostname), `provider_type`/`provider_name` (data source -- flag Import API-sourced rows distinctly from real Tanium Client rows), plus whatever other fields the row actually has; the rest of the field shape is not fully confirmed, so render what's present rather than assuming a fixed column set.
- **Comply findings** -- endpointName, rule, standard, state, lastScanDate.
- **Comply CVEs** -- endpointName, cveId, the version-appropriate score field (cvssScore / cvssScoreV3 / cvssScoreV4), isCisaKev.
- **Patch definitions** -- title, severity, platform, releaseDate, isSuperseded.
- **Patch deployment** -- name, status, platform, schedule.startTime/endTime.
- **Threat Response alert** -- render whatever fields the live payload actually contains; the shape here is unconfirmed.

≤5 rows -> inline table. More than that -> show the first several plus a count of remaining matches, and offer to narrow the filter or `--computer-group`.

## Error handling

- **GraphQL `errors` array in a 200 response** -- Gateway (like any GraphQL service) can report a failed query with HTTP 200 and a top-level `errors` array instead of a non-2xx status. This connector checks for that explicitly and raises on it; if you see "Gateway returned GraphQL errors", read the embedded error text rather than assuming it's an HTTP-layer problem.
- **401/403 on a Gateway call** -- the API token is invalid/expired, or the persona/role bound to it lacks RBAC read access to that module (Patch specifically needs distinct named permissions -- Patch Show, Patch Deployment Read -- see Subcommands table). Surface this as a configuration issue for whoever set up the connector, not something the agent can work around.
- **403/404 on a Threat Response call** -- given how unverified this transport is, a failure here could equally mean a wrong endpoint path (API version drift) as a real auth/permission problem. Don't assume it's a permissions issue without also considering the path itself may be stale.
- **Empty compliance/CVE findings for a scoped computer group** -- a real, correct empty result if no matching policy/scan has run yet, not necessarily a query bug. Suggest checking the Comply workbench for whether a scan has actually completed for that group before concluding the query is wrong.
- **429/5xx** -- upstream throttling or a Tanium Cloud service issue (Gateway docs state it has no specific rate limit, so a 429 here is more likely a shared-infrastructure limit); back off and report rather than retrying in a loop.

## Guardrails

- No write actions exist on this connector -- it is read-only by design. Do not imply Asset upsert, Patch deployment create/stop, Patch list changes, or Threat Response alert resolution are possible through it.
- `query-patch-applicability` requires `--computer-group` -- this connector refuses an unscoped, tenant-wide sensor read by design (the underlying sensor mechanism has no server-side bound otherwise).
- Do not fabricate rows when a query returns zero results -- report zero results plainly and suggest loosening the filter or widening `--computer-group`.
- Do not treat a Comply/Patch finding's absence as "resolved" without checking `lastScanDate`/`createdDate` for staleness -- an old scan can mean the finding is stale, not that it no longer applies.
- Do not construct or print the `session` header, `api_host`, or `api_token`.
- Treat every Threat Response claim (endpoint shape, filter support, pagination) as unconfirmed per the Confidence levels table -- don't state its behavior as fact to the user.

## Connector base URL

Configure the connector's base URL as the customer's **Tanium Cloud API host** (the `-api` subdomain used by Gateway, e.g. `acme-api.cloud.tanium.com`) -- no scheme, no path. The CLI sends absolute paths per call (`/plugin/products/gateway/graphql` for Asset/Comply/Patch, `/plugin/products/detect3/api/v1/alerts...` for Threat Response).

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- the skill auto-resolves its bound connector; the server applies the `session` header from the connector's stored API token.

## Output format

Query commands return grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). `get-*`, `view-result`, and KG commands return JSON.
