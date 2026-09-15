---
name: intune
description: Drive a Microsoft Intune connector via the `intune.py` CLI. Search managed devices by OData $filter, drill into a result, hydrate an entity by id, walk the catalog into the knowledge graph, and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Microsoft Intune

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/intune.py`. Run `scripts/intune.py --help` for the subcommand list; `scripts/intune.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Only the `managed-devices` resource is implemented -- do not attempt other `deviceManagement` sub-resources (`deviceCompliancePolicies`, `deviceConfigurations`, `mobileApps`, etc.) or the `/beta` Graph surface; none are wired into this connector's `RESOURCES` map. This connector is read-only (`GET` only) -- there are no write/remediation actions (retire, wipe, sync) available through it.

## Auth recap

The connector stores three fields -- `tenant_id`, `client_id`, `client_secret` -- and crogld exchanges them server-side for a bearer token via the tenant's `https://login.microsoftonline.com/<tenant_id>/oauth2/v2.0/token` endpoint (client-credentials grant, `https://graph.microsoft.com/.default` scope), then attaches the `Authorization: Bearer` header automatically on every call. Never ask the user for the tenant ID, client ID, or client secret during analysis, never print them, and never construct Graph auth headers manually.

## Tool call shape

```bash
intune.py query --resource managed-devices --filter "complianceState eq 'noncompliant'" --limit 25
intune.py get --resource managed-devices --id <device-id>
intune.py view-result --ref <ref> --row 1
intune.py grep --pattern hostname
intune.py ls --path /
```

`--limit` is **required** on `query` -- there is no default, and the CLI errors immediately if it's omitted. It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped. See Context budget rules below for what to pick. `view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `intune.py query` (`--resource`, `--filter`, **`--limit` required**, `--output`) | Run an OData `$filter` search over a resource (`--resource managed-devices`). Paginates via `@odata.nextLink` up to `--limit`; returns grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `intune.py grep` | String-search this connector's knowledge-graph catalog — matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes (populated by `refresh` + the kg-learner describe pass), not the live API; for cross-connector search use `kg_learner.py grep`. |
| `intune.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full entity JSON). Pure cache read — Graph entities are returned in full at search time. |
| `intune.py get` | Hydrate one entity by id (`--resource`, `--id`); no prior query ref needed. |
| `intune.py upload-samples` | Upload sample entities to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (a `--output` artifact), plus the exact `--filter` value as `--query` used to fetch them, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or when the read-before-write check fails. |
| `intune.py refresh` | Walk every configured Intune connector's catalog (resource → field) and commit each tier to the KG via `commit_walk_tier`. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. The only producer path — `ls` reads from KG nodes exclusively. Run after configuring a new connector; the weekly `kg-schema-refresh` scheduled job invokes this automatically. |
| `intune.py ls --path /<abs-path> --format json` | List this skill's Intune connectors' database schema by UNIX path (Resource → Field). Field names are discovered empirically from a one-row sample of each resource. JSON output; consumed by the `kg_learner.py ls` aggregator. For human-friendly Markdown / PSV output, call `kg_learner.py ls` instead. |

## Context budget rules

- Default to a small `--limit` (25 is a reasonable starting point for an analyst-facing question). Only raise it on explicit user request, and prefer narrowing `--filter` first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, so there's no "just set --limit very high" option for bulk population; run `refresh`/multiple narrower `query`s instead.
- An empty `--filter` still returns a broader unfiltered slice for knowledge-graph population (see Uploading samples), but it's bounded by the same 100-row hard cap as everything else -- for an analyst-facing question, add a filter when the user names a device, compliance state, or OS instead of fetching broadly and filtering client-side.
- Render the main device fields per Rendering pattern below; do not paste full raw JSON for more than a couple of devices inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## OData filters

`--filter` is an OData v4 `$filter` expression — e.g. `complianceState eq 'noncompliant'`, `operatingSystem eq 'Windows'`, or `lastSyncDateTime le 2024-01-01T00:00:00Z`. An empty `--filter` returns all rows up to `--limit`; this is the default and preferred approach — an unfiltered walk is what best populates the knowledge graph across the full device inventory. Quote the whole expression in the shell.

OData has no native in-list operator for equality; a lookup against multiple values is sent as a chain of `field eq 'v1' or field eq 'v2' ...`.

Commonly useful, documented `managedDevices` properties:

| Field | Example | Notes |
|---|---|---|
| `complianceState` | `complianceState eq 'noncompliant'` | Values: `compliant`, `noncompliant`, `conflict`, `error`, `inGracePeriod`, `notApplicable`, `unknown`. |
| `operatingSystem` | `operatingSystem eq 'Windows'` | Also `iOS`, `Android`, `macOS`, etc. |
| `managementState` | `managementState eq 'managed'` | Enrollment/management lifecycle state. |
| `deviceName` | `deviceName eq '<name>'` | Exact match only via `eq` -- see substring note below. |
| `userPrincipalName` | `userPrincipalName eq '<upn>'` | Primary user of the device. |
| `serialNumber` | `serialNumber eq '<serial>'` | Hardware serial. |

**Field-level `$filter` support on this Graph endpoint is not uniform across all properties, and this has not been live-verified against a real tenant.** If a filter returns `400 Request_UnsupportedQuery` (a real, documented Graph error for a property that isn't filterable on this endpoint), don't retry the same filter -- drop it, fetch unfiltered (or filtered on a supported property) with a small `--limit`, and filter the result client-side instead.

There is no substring/`contains()` recipe validated here -- if the user needs a partial-name match and an exact `eq` filter returns nothing, fall back to an unfiltered `--limit` fetch and filter client-side rather than guessing at `contains()`/`startswith()` syntax that hasn't been confirmed against this endpoint.

## Resources

Only `managed-devices` (`/v1.0/deviceManagement/managedDevices`) is implemented today. Intune's broader `deviceManagement` surface (compliance policies, configuration profiles, mobile apps) spans both the stable `/v1.0` and the unversioned `/beta` Graph API, so each resource this skill adds carries its own explicit version in its path rather than inheriting one from the connector's base URL. Every request also sends an explicit `$top` (capped at the same 100-row hard cap) rather than relying on Graph's own default page size.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded entity must be the byte-identical output of a recent `query` issued from this same container. The round-trip (query → see results → choose to upload) is what forces the LLM to be judicious about what is sampled. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** — capture the `Result reference: <uuid>` line that `query` prints on stdout and pass that UUID. The container-local cache holds the entities that the query just returned.
2. **`--from-file FILE`** — when the prior `query` was invoked with `--output FILE`, pass that file. Its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

In either form the **exact same `--filter` value (passed as `--query`)** must be passed to `upload-samples` as were passed to the originating `query`; mismatched values fail the read-before-write check. (Pass `--query ""` when the query used no filter.)

The upload cap is **10 rows** (`MAX_UPLOAD_ROWS=10`). Tighter filters are the expected pattern — `--limit 1` is what to reach for first when sampling a single resource. The point is one or a handful of exemplary entities per leaf, not bulk data.

`--catalog-path` (required) is the inventory path under which the sample is recorded — for Intune, typically `/<resource>/<field>` (e.g. `/managed-devices/complianceState`). The first tier is the Intune resource. Tiers are created lazily server-side, and the server walks each sample's nested JSON keys into child nodes under the leaf, so the whole device object shape becomes catalog nodes automatically.

## Edge-connectable fields

When picking source or target nodes for `kg_learner.py create-edge`, use **scalar identity fields** from a sampled entity. The server's inner-join check intersects bare JSON string leaves between two `data_samples`; a managed device exposes several:

- Device: `deviceName`, `id`, `serialNumber`, `imei`, `azureADDeviceId`
- Network: `wiFiMacAddress`, `ethernetMacAddress`
- Identity: `userPrincipalName`, `emailAddress`, `userId`
- Identity (cross-system join targets): email addresses, MAC addresses, serial numbers — these are the values that line up with a CrowdStrike host, a Sentinel device entity, or a ServiceNow CI on another connector.

**Do not edge against free prose** like `notes` or human-written narrative fields — the inner-join requires whole-leaf equality, so an identifier embedded in prose won't match a bare leaf from another connector.

## Rendering pattern

Render a compact card or table per device -- don't dump raw PSV/JSON as the answer:

- **Device name** -- `deviceName`
- **Compliance** -- `complianceState`
- **OS** -- `operatingSystem` / `osVersion`
- **Last sync** -- `lastSyncDateTime`
- **Primary user** -- `userPrincipalName` or `emailAddress`
- **Serial / model** -- `serialNumber`, `model`, `manufacturer`
- **Device id** -- `id` (needed for a follow-up `get` or `view-result`)

≤5 devices → inline table. More than that → show the first several plus a count of remaining matches, and offer to narrow the filter.

## Error handling

- **400 `Request_UnsupportedQuery`** -- the `$filter` uses a property or operator Graph doesn't support on this endpoint. Drop the filter (or switch to a documented-supported property from the table above) rather than retrying the same expression.
- **401** -- bearer token invalid/expired; this should self-heal via the connector's OAuth flow. If it persists, surface that the connector's stored credential may be invalid or revoked -- do not ask the user to paste a token into chat.
- **403** -- the app registration lacks the Graph permission (e.g. `DeviceManagementManagedDevices.Read.All`) or admin consent hasn't been granted. Surface this as a configuration issue for whoever set up the connector, not something the agent can work around.
- **429** -- Graph is throttling; back off and report the rate limit rather than retrying in a loop. Graph's `Retry-After` header (if present) indicates how long to wait.
- **5xx** -- upstream Graph service issue; report the status and retry only if the user asks.

## Guardrails

- No write actions exist on this connector -- it is read-only (`GET` only). Do not imply Intune remediation (retire, wipe, sync) is possible through it.
- Do not treat `complianceState` alone as a full compliance verdict without checking `lastSyncDateTime` for staleness -- an old sync can mean the compliance state itself is stale, not necessarily accurate right now.
- Do not expose or reconstruct the tenant ID, client ID, or client secret.
- Do not fabricate device data when a query returns zero results -- report zero results plainly and suggest loosening the filter.

## Connector base URL

Configure the connector's base URL as the **Graph API root**: `https://graph.microsoft.com`. The CLI sends absolute, per-resource versioned paths (`/v1.0/deviceManagement/managedDevices`, …) rather than relying on a version baked into the connector's base URL.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets — the skill auto-resolves its bound connector; the server applies the OAuth Bearer stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
