---
name: purview-dlp
description: Drive a Microsoft Purview DLP connector via the `purview_dlp.py` CLI. Search Data Loss Prevention alerts by OData $filter, drill into a result, hydrate an alert by id, walk the catalog into the knowledge graph, and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Microsoft Purview DLP

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/purview_dlp.py`. Run `scripts/purview_dlp.py --help` for the subcommand list; `scripts/purview_dlp.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Only the `dlp-alerts` resource is implemented, over the unified Microsoft Graph security alerts_v2 surface (`serviceSource eq 'dataLossPrevention'`) -- do not attempt Purview's other surfaces (Audit, eDiscovery, Data Map) through this connector; none are wired in. This connector is read-only (`GET` only) -- there is no alert-update action (status, classification, comments) available through it.

## Auth recap

The connector stores three fields -- `tenant_id`, `client_id`, `client_secret` -- and crogld exchanges them server-side for a bearer token via the tenant's `https://login.microsoftonline.com/<tenant_id>/oauth2/v2.0/token` endpoint (client-credentials grant, `https://graph.microsoft.com/.default` scope), then attaches the `Authorization: Bearer` header automatically on every call. Never ask the user for the tenant ID, client ID, or client secret during analysis, never print them, and never construct Graph auth headers manually.

## Tool call shape

```bash
purview_dlp.py query --filter "severity eq 'high'" --limit 25
purview_dlp.py get --id <alert-id>
purview_dlp.py view-result --ref <ref> --row 1
purview_dlp.py grep --pattern sharepoint
purview_dlp.py ls --path /
```

`--limit` is **required** on `query` -- there is no default, and the CLI errors immediately if it's omitted. It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped. See Context budget rules below for what to pick. `view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `purview_dlp.py query` (`--filter`, **`--limit` required**, `--output`) | Run an OData `$filter` search over `dlp-alerts`. The CLI always ANDs `serviceSource eq 'dataLossPrevention'` onto whatever filter is passed -- this connector can never widen a query to non-DLP alerts. Paginates via `@odata.nextLink` up to `--limit`; returns grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `purview_dlp.py grep` | String-search this connector's knowledge-graph catalog — matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes (populated by `refresh` + the kg-learner describe pass), not the live API; for cross-connector search use `kg_learner.py grep`. |
| `purview_dlp.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full alert JSON). Pure cache read — Graph entities are returned in full at search time. |
| `purview_dlp.py get` | Hydrate one alert by id (`--id`); no prior query ref needed. Note: unlike `query`, `get` does not re-check `serviceSource` -- Graph's get-by-id has no `$filter` support, so a caller who already holds a concrete alert id from elsewhere (e.g. an incident cross-reference) can fetch it directly even if it isn't DLP-sourced. |
| `purview_dlp.py upload-samples` | Upload sample alerts to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (a `--output` artifact), plus the exact `--filter` value as `--query` used to fetch them, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or when the read-before-write check fails. |
| `purview_dlp.py refresh` | Walk this connector's catalog (resource → field) and commit each tier to the KG via `commit_walk_tier`. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. The only producer path — `ls` reads from KG nodes exclusively. Run after configuring a new connector; the weekly `kg-schema-refresh` scheduled job invokes this automatically. |
| `purview_dlp.py ls --path / --format json` | List this skill's Purview DLP connectors' database schema by UNIX path (Resource → Field). Field names are discovered empirically from a one-row sample of the resource. JSON output; consumed by the `kg_learner.py ls` aggregator. For human-friendly Markdown / PSV output, call `kg_learner.py ls` instead. |

## Context budget rules

- Default to a small `--limit` (25 is a reasonable starting point for an analyst-facing question). Only raise it on explicit user request, and prefer narrowing `--filter` first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, so there's no "just set --limit very high" option for bulk population; run multiple narrower `query`s instead.
- An empty `--filter` still returns a broader unfiltered slice for knowledge-graph population (see Uploading samples), but it's bounded by the same 100-row hard cap as everything else -- for an analyst-facing question, add a filter when the user names a severity, status, or time range instead of fetching broadly and filtering client-side.
- Render the main alert fields per Rendering pattern below; do not paste full raw JSON for more than a couple of alerts inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## OData filters

`--filter` is an OData v4 `$filter` expression over the properties Microsoft Graph documents as filterable on `alerts_v2`: `assignedTo`, `classification`, `determination`, `createdDateTime`, `lastUpdateDateTime`, `severity`, `status`. (`serviceSource` is also filterable on the underlying endpoint, but this CLI owns that clause -- see below.) An empty `--filter` returns all DLP alerts up to `--limit`; this is the default and preferred approach for populating the knowledge graph. Quote the whole expression in the shell.

**This CLI always ANDs `serviceSource eq 'dataLossPrevention'` onto every `query` request**, server-side in `purview_dlp.py`, not just as documentation guidance -- there is no flag to disable it. This is what makes `purview-dlp` a DLP-only connector rather than a general Graph-alerts client; a broader alerts connector covering other `serviceSource` providers (Defender for Endpoint, Sentinel, Entra ID Protection, etc.) would be a separate connector type. The CLI rejects a `--filter` whose parentheses aren't properly balanced, because an unbalanced fragment could otherwise recombine with the parens the CLI adds around it and slip a clause out from under that `and` -- if you hit a "has unmatched" or "unbalanced parentheses" error, check the expression's parens rather than retrying as-is. A third error, "has an unterminated string literal," means a `'` inside a value wasn't doubled -- e.g. `assignedTo eq 'O'Brien'` needs to be `assignedTo eq 'O''Brien'` (OData escapes an embedded `'` by doubling it, not with a backslash); that one's a quoting fix, not a parens fix.

OData has no native in-list operator for equality; a lookup against multiple values is sent as a chain of `field eq 'v1' or field eq 'v2' ...`, with each value escaped for OData string-literal syntax.

Commonly useful, documented `alerts_v2` properties:

| Field | Example | Notes |
|---|---|---|
| `severity` | `severity eq 'high'` | Values: `unknown`, `informational`, `low`, `medium`, `high`. |
| `status` | `status eq 'new'` | Values: `unknown`, `new`, `inProgress`, `resolved`. |
| `classification` | `classification eq 'truePositive'` | Values: `unknown`, `falsePositive`, `truePositive`, `informationalExpectedActivity`. |
| `determination` | `determination eq 'phishing'` | See the `alertDetermination` enum for the full value set; not all determinations are meaningful for DLP alerts specifically. |
| `createdDateTime` | `createdDateTime ge 2026-07-01T00:00:00Z` | ISO 8601, UTC. |
| `lastUpdateDateTime` | `lastUpdateDateTime ge 2026-07-01T00:00:00Z` | ISO 8601, UTC. |
| `assignedTo` | `assignedTo eq null` | Owner of the alert, or unassigned. |

**Whether Graph's DLP-specific rule-match enrichment (policy name, matched rule, sensitive-information-type detections) surfaces as its own field or inside the generic `additionalData`/`customDetails` dictionaries has not been live-verified against a real tenant.** Treat any such fields you see in a live response as authoritative for that tenant, but don't assume a specific field name (e.g. `dlpRuleMatch`) exists before checking an actual response -- render whatever `additionalData`/`customDetails` contain rather than guessing a key.

## Resources

Only `dlp-alerts` (`/v1.0/security/alerts_v2`, filtered to `serviceSource eq 'dataLossPrevention'`) is implemented. This is one provider's slice of Microsoft Graph's unified security alerts_v2 surface, which also carries alerts from Defender, Entra ID Protection, Sentinel, and others -- this connector deliberately only ever returns the DLP slice. Every request also sends an explicit `$top` (capped at the same 100-row hard cap) rather than relying on the API's own default page size, and pagination via `@odata.nextLink` stops as soon as the (clamped) `--limit` is reached.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded entity must be the byte-identical output of a recent `query` issued from this same container. The round-trip (query → see results → choose to upload) is what forces the LLM to be judicious about what is sampled. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** — capture the `Result reference: <uuid>` line that `query` prints on stdout and pass that UUID. The container-local cache holds the entities that the query just returned.
2. **`--from-file FILE`** — when the prior `query` was invoked with `--output FILE`, pass that file. Its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

In either form the **exact same `--filter` value (passed as `--query`)** must be passed to `upload-samples` as were passed to the originating `query`; mismatched values fail the read-before-write check. (Pass `--query ""` when the query used no filter.)

The upload cap is **10 rows** (`MAX_UPLOAD_ROWS=10`). Tighter filters are the expected pattern — `--limit 1` is what to reach for first when sampling. The point is one or a handful of exemplary alerts, not bulk data.

`--catalog-path` (required) is the inventory path under which the sample is recorded — for Purview DLP, `/dlp-alerts/<field>` (e.g. `/dlp-alerts/severity`). The first tier is the resource. Tiers are created lazily server-side, and the server walks each sample's nested JSON keys into child nodes under the leaf, so the whole alert object shape becomes catalog nodes automatically.

## Edge-connectable fields

When picking source or target nodes for `kg_learner.py create-edge`, use **scalar identity fields** from a sampled alert. The server's inner-join check intersects bare JSON string leaves between two `data_samples`; a DLP alert exposes several:

- Alert: `id`, `providerAlertId`, `incidentId`, `detectorId`, `tenantId`
- Identity (cross-system join targets): values inside `evidence[]` entries such as a `userEvidence` account UPN, a `mailboxEvidence` address, or a `cloudApplicationEvidence` app id -- these are what line up with a user or mailbox entity on another connector.

**Do not edge against free prose** like `description` or `recommendedActions` -- the inner-join requires whole-leaf equality, so an identifier embedded in prose won't match a bare leaf from another connector.

## Rendering pattern

Render a compact card or table per alert -- don't dump raw PSV/JSON as the answer:

- **Title** -- `title`
- **Severity** -- `severity`
- **Status** -- `status`
- **Classification / determination** -- `classification`, `determination`
- **Created / last updated** -- `createdDateTime`, `lastUpdateDateTime`
- **Description** -- `description` (truncate long text)
- **Alert id** -- `id` (needed for a follow-up `get` or `view-result`)
- **Portal link** -- `alertWebUrl`, if present

≤5 alerts → inline table. More than that → show the first several plus a count of remaining matches, and offer to narrow the filter.

## Error handling

- **400 `Request_UnsupportedQuery`** -- the `$filter` uses a property or operator Graph doesn't support on this endpoint. Drop the filter (or switch to a documented-supported property from the table above) rather than retrying the same expression.
- **401** -- bearer token invalid/expired; this should self-heal via the connector's OAuth flow. If it persists, surface that the connector's stored credential may be invalid or revoked -- do not ask the user to paste a token into chat.
- **403 `Authorization_RequestDenied` (or similar insufficient-privileges message)** -- the app registration lacks the `SecurityAlert.Read.All` Graph permission or admin consent hasn't been granted. Surface this as a configuration issue for whoever set up the connector, not something the agent can work around.
- **403 `Unauthorized` / "Account is not provisioned"** -- a distinct 403 from the one above, seen even with `SecurityAlert.Read.All` correctly granted and consented. This means the tenant's unified Microsoft Defender alerts-and-incidents data plane (which `alerts_v2` and `/security/incidents` both read from) isn't provisioned -- confirmed live against an internal test tenant, this persisted through a correct permission grant, a Defender portal visit, and a real DLP policy match, and only started resolving once the tenant's Unified Audit Log (previously off) was turned on, with up to 24h to fully propagate. Don't retry this in a loop or treat it as a connector bug; surface it as a tenant-provisioning prerequisite (see the provisioning guide's Troubleshooting section).
- **429** -- Graph is throttling; back off and report the rate limit rather than retrying in a loop. Graph's `Retry-After` header (if present) indicates how long to wait.
- **5xx** -- upstream Graph service issue; report the status and retry only if the user asks.

## Guardrails

- No write actions exist on this connector -- it is read-only (`GET` only). Do not imply alert triage actions (status change, classification, comments) are possible through it.
- Do not treat a `classification` of `unknown` or a `determination` of `unknown` as evidence of anything -- it means the alert hasn't been triaged yet, not that it's benign.
- Do not expose or reconstruct the tenant ID, client ID, or client secret.
- Do not fabricate alert data when a query returns zero results -- report zero results plainly and suggest loosening the filter.
- Do not invent field names for DLP rule-match details (policy name, matched sensitive-info type, etc.) -- render whatever the live response actually contains under `additionalData`/`customDetails`/`evidence`, and say so plainly if those are empty for a given alert.

## Connector base URL

Configure the connector's base URL as the **Graph API root**: `https://graph.microsoft.com`. The CLI sends absolute, versioned paths (`/v1.0/security/alerts_v2`, …) rather than relying on a version baked into the connector's base URL.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets — the skill auto-resolves its bound connector; the server applies the OAuth Bearer stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
