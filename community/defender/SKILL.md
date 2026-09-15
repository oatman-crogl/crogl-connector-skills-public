---
name: defender
description: Drive a Microsoft Defender for Endpoint connector via the `defender.py` CLI. Search alerts and machines by OData $filter, drill into a result, hydrate an entity by id, walk the catalog into the knowledge graph, and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Microsoft Defender for Endpoint

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/defender.py`. Run `scripts/defender.py --help` for the subcommand list; `scripts/defender.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Only `alerts` and `machines` are implemented -- do not attempt other Defender for Endpoint API surfaces (`indicators`, `investigations`, `recommendations`, etc.); none are wired into this connector's `RESOURCES` map. This connector is read-only (`GET` only) -- there are no write/remediation actions (isolate device, run AV scan, resolve alert) available through it.

## Auth recap

The connector stores three fields -- `tenant_id`, `client_id`, `client_secret` -- and crogld exchanges them server-side for a bearer token via the tenant's `https://login.microsoftonline.com/<tenant_id>/oauth2/v2.0/token` endpoint (client-credentials grant, `https://api.securitycenter.microsoft.com/.default` scope), then attaches the `Authorization: Bearer` header automatically on every call. Never ask the user for the tenant ID, client ID, or client secret during analysis, never print them, and never construct auth headers manually.

## Tool call shape

```bash
defender.py query --resource alerts --filter "severity eq 'High'" --limit 25
defender.py query --resource machines --limit 25
defender.py get --resource machines --id <device-id>
defender.py view-result --ref <ref> --row 1
defender.py grep --pattern riskScore
defender.py ls --path /
```

`--limit` is **required** on `query` -- there is no default, and the CLI errors immediately if it's omitted. It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped. See Context budget rules below for what to pick. `view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`. `--resource` on both `query` and `get` defaults to `alerts` -- always pass `--resource machines` explicitly when asking about devices, or a machine-focused question will silently query the wrong resource.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `defender.py query` (`--resource`, `--filter`, **`--limit` required**, `--output`) | Run an OData `$filter` search over a resource (`--resource alerts\|machines`, defaults to `alerts`) through a custom connector. Paginates via `@odata.nextLink` up to `--limit`; returns grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `defender.py grep` | String-search this connector's knowledge-graph catalog — matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes (populated by `refresh` + the kg-learner describe pass), not the live API; for cross-connector search use `kg_learner.py grep`. |
| `defender.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full entity JSON). Pure cache read — Defender entities are returned in full at search time. |
| `defender.py get` (`--resource` defaults to `alerts`, `--id` required) | Hydrate one entity by id; no prior query ref needed. |
| `defender.py upload-samples` | Upload sample entities to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (a `--output` artifact), plus the exact `--filter` value as `--query` used to fetch them, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or when the read-before-write check fails. |
| `defender.py refresh` | Walk every configured Defender connector's catalog (resource → field) and commit each tier to the KG via `commit_walk_tier`. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. The only producer path — `ls` reads from KG nodes exclusively. Run after configuring a new connector; the weekly `kg-schema-refresh` scheduled job invokes this automatically. |
| `defender.py ls --path /<abs-path> --format json` | List this skill's Defender connectors' database schema by UNIX path (Resource → Field). Field names are discovered empirically from a one-row sample of each resource. JSON output; consumed by the `kg_learner.py ls` aggregator. For human-friendly Markdown / PSV output, call `kg_learner.py ls` instead. |

## Context budget rules

- `--limit` is mandatory on `query`; default to a small value (25 is a reasonable starting point for an analyst-facing question). Only raise it on explicit user request, and prefer narrowing `--filter` first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, so there's no "just set --limit very high" option for bulk population; run `refresh`/multiple narrower `query`s instead.
- An empty `--filter` still returns a broader unfiltered slice for knowledge-graph population (see Uploading samples), but it's bounded by the same 100-row hard cap as everything else -- for an analyst-facing question, add a filter when the user names a severity, status, or device instead of fetching broadly and filtering client-side.
- Render the main fields per Rendering pattern below; do not paste full raw JSON for more than a couple of entities inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## OData filters

`--filter` is an OData v4 `$filter` expression — e.g. `severity eq 'High'`, `status eq 'New'`, or `healthStatus eq 'Active'`. An empty `--filter` returns all rows up to `--limit`; this is the default and preferred approach — an unfiltered walk is what best populates the knowledge graph across the full alert/device population. Quote the whole expression in the shell.

OData has no native in-list operator for equality; a lookup against multiple values is sent as a chain of `field eq 'v1' or field eq 'v2' ...`.

Commonly useful, documented properties:

**Alerts:**

| Field | Example | Notes |
|---|---|---|
| `severity` | `severity eq 'High'` | Values: `Informational`, `Low`, `Medium`, `High`. |
| `status` | `status eq 'New'` | Values: `New`, `InProgress`, `Resolved`. |
| `classification` | `classification eq 'TruePositive'` | Values: `Unknown`, `FalsePositive`, `TruePositive`. |
| `machineId` | `machineId eq '<id>'` | Join key back to `machines`. |

**Machines:**

| Field | Example | Notes |
|---|---|---|
| `healthStatus` | `healthStatus eq 'Active'` | Values include `Active`, `Inactive`, `ImpairedCommunication`, `NoSensorData`. |
| `riskScore` | `riskScore eq 'High'` | Values: `None`, `Informational`, `Low`, `Medium`, `High`. |
| `exposureLevel` | `exposureLevel eq 'Medium'` | Values: `None`, `Low`, `Medium`, `High`. |
| `computerDnsName` | `computerDnsName eq '<hostname>'` | Exact match only via `eq`. |
| `osPlatform` | `osPlatform eq 'Windows10'` | Platform string as reported by the sensor. |

**Field-level `$filter` support is not uniform across all properties, and this has not been live-verified against a real tenant.** If a filter returns a 400-class error for an unsupported property or operator, don't retry the same filter -- drop it, fetch unfiltered (or filtered on a supported property) with a small `--limit`, and filter the result client-side instead.

## Resources

Two resources are implemented, mirroring CrowdStrike's alerts/hosts split:

- `alerts` (`/api/alerts`) — security alerts: severity, status, classification, determination, detection source, associated device.
- `machines` (`/api/machines`) — onboarded device inventory: health status, risk score, exposure level, OS, network identifiers.

Unlike Falcon's two-stage query→hydrate pattern, both Defender endpoints return full entities directly, paginated via `@odata.nextLink`. Every request also sends an explicit `$top` (capped at the same 100-row hard cap) rather than relying on the API's own default page size.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded entity must be the byte-identical output of a recent `query` issued from this same container. The round-trip (query → see results → choose to upload) is what forces the LLM to be judicious about what is sampled. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** — capture the `Result reference: <uuid>` line that `query` prints on stdout and pass that UUID. The container-local cache holds the entities that the query just returned.
2. **`--from-file FILE`** — when the prior `query` was invoked with `--output FILE`, pass that file. Its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

In either form the **exact same `--filter` value (passed as `--query`)** must be passed to `upload-samples` as were passed to the originating `query`; mismatched values fail the read-before-write check. (Pass `--query ""` when the query used no filter.)

The upload cap is **10 rows** (`MAX_UPLOAD_ROWS=10`). Tighter filters are the expected pattern — `--limit 1` is what to reach for first when sampling a single resource. The point is one or a handful of exemplary entities per leaf, not bulk data.

`--catalog-path` (required) is the inventory path under which the sample is recorded — for Defender, typically `/<resource>/<field>` (e.g. `/machines/riskScore`). The first tier is the Defender resource. Tiers are created lazily server-side, and the server walks each sample's nested JSON keys into child nodes under the leaf, so the whole alert/machine object shape becomes catalog nodes automatically.

## Edge-connectable fields

When picking source or target nodes for `kg_learner.py create-edge`, use **scalar identity fields** from a sampled entity. The server's inner-join check intersects bare JSON string leaves between two `data_samples`; Defender entities expose several:

- Device (from `machines`): `computerDnsName`, `id`, `aadDeviceId`, `lastIpAddress`, `lastExternalIpAddress`
- Alert-to-device link (from `alerts`): `machineId`, `computerDnsName`
- Identity: `aadDeviceId` lines up with an Entra ID device id from other Microsoft-stack connectors (e.g. Intune's `managed-devices`).
- Identity (cross-system join targets): hostnames, IPs, device ids — these are the values that line up with a CrowdStrike host, an Intune managed device, or a Sentinel entity on another connector.

**Do not edge against free prose** like `description` or `determination` narrative fields — the inner-join requires whole-leaf equality, so an identifier embedded in prose won't match a bare leaf from another connector.

## Rendering pattern

Render a compact table -- don't dump raw PSV/JSON as the answer:

- **Alerts** -- severity · status · title · classification · machineId/computerDnsName · alertCreationTime.
- **Machines** -- computerDnsName · healthStatus · riskScore · exposureLevel · osPlatform · lastSeen.

≤5 rows → inline table. More than that → show the first several plus a count of remaining matches, and offer to narrow the filter.

## Error handling

- **400** -- the `$filter` uses a property or operator the API doesn't support. Drop the filter (or switch to a documented-supported property from the table above) rather than retrying the same expression.
- **401** -- bearer token invalid/expired; this should self-heal via the connector's OAuth flow. If it persists, surface that the connector's stored credential may be invalid or revoked -- do not ask the user to paste a token into chat.
- **403** -- the app registration lacks the required Defender for Endpoint API permission (`Alert.Read.All` for alerts, `Machine.Read.All` for machines -- see the provisioning guide for a documented case where Microsoft's own API reference disagrees with itself on whether alerts needs the broader `Alert.ReadWrite.All`) or admin consent hasn't been granted. Surface this as a configuration issue for whoever set up the connector, not something the agent can work around.
- **404 on `machines`** -- this is Microsoft's documented response for "no recent machines" on this endpoint, not an error to retry -- unlike `alerts`, which returns `200` with an empty `value` array when there's nothing to report. Report it as "no onboarded devices yet" rather than a query failure.
- **429** -- the API is throttling; back off and report the rate limit rather than retrying in a loop.
- **5xx** -- upstream service issue; report the status and retry only if the user asks.

## Guardrails

- No write actions exist on this connector -- it is read-only (`GET` only). Do not imply remediation (isolate device, run scan, resolve alert) is possible through it.
- Do not treat a single alert's `classification`/`determination` as a final verdict without corroborating evidence -- these are analyst-set fields that may be unset (`Unknown`) or stale.
- Do not expose or reconstruct the tenant ID, client ID, or client secret.
- Do not fabricate alert or device data when a query returns zero results -- report zero results plainly and suggest loosening the filter.
- **Routing note:** if a customer already has Defender data flowing into Microsoft Sentinel, that's a separate, complementary data path (Sentinel gets ingested alerts/incidents, often with some lag; this connector gives live alert/machine state, including device inventory fields Sentinel may not have ingested). Don't assume one supersedes the other without checking what that specific customer has configured.

## Connector base URL

Configure the connector's base URL as the **Defender for Endpoint API root**: `https://api.securitycenter.microsoft.com`. The CLI sends absolute API paths (`/api/alerts`, `/api/machines`, …).

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets — the skill auto-resolves its bound connector; the server applies the OAuth Bearer stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
