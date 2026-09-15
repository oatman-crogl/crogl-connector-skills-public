---
name: tenable-io
description: Drive a Tenable Vulnerability Management (Tenable.io) connector via the `tenable_io.py` CLI. Search vulnerability findings aggregated by plugin, drill into a plugin's per-asset detail, walk the catalog into the knowledge graph, and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Tenable Vulnerability Management (Tenable.io)

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/tenable_io.py`. Run `scripts/tenable_io.py --help` for the subcommand list; `scripts/tenable_io.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Only the `workbenches/vulnerabilities` surface is implemented -- do not attempt other Tenable.io resources (`assets`, `scans`, `plugins`, `was` web-app scanning, `container-security`, etc.) or the async `/vulns/export`, `/assets/v2/export` bulk-export job-poll endpoints; none are wired into this connector. This connector is read-only (`GET` only) -- there are no write/remediation actions (accept risk, recast severity, launch a scan) available through it.

**This is Tenable Vulnerability Management (Tenable.io) specifically**, not Tenable Security Center (self-hosted) or Tenable Identity Exposure (formerly Tenable.ad) -- those are separate products with separate APIs and would need separate connector types.

## Auth recap

The connector stores two fields -- `access_key` and `secret_key` -- and crogld sends them server-side as a single `X-ApiKeys: accessKey=<access_key>; secretKey=<secret_key>;` header on every call. Tenable requires both values in that one header, and this connector binding can only encrypt one field per header (the `{{secret}}` placeholder), so `secret_key` is the encrypted field and `access_key` is inlined as plain config -- this is the intended, code-verified design of this connector's `credential_schema.binding`, not an oversight; see connector.json's field descriptions. Never ask the user for either key during analysis, never print them, and never construct the `X-ApiKeys` header manually.

**This composite-header binding shape (one `{{secret}}` token plus an inlined non-secret token in the same header template) has no prior precedent elsewhere in this repo.** It is confirmed valid against `croglng`'s binding validator (`internal/skills/manifest.go`) and passes `validate_connector.py`, but has not been exercised end-to-end against a live request -- confirm the `X-ApiKeys` header actually reaches Tenable correctly the first time this connector is live-tested (see Phase 6a in this repo's `/build-connector` skill), and fold any correction back into this file.

## Tool call shape

```bash
tenable_io.py query --filter severity eq Critical --limit 25
tenable_io.py query --filter plugin.name match OpenSSL --date-range 90 --limit 25
tenable_io.py get --plugin-id 51192 --limit 25
tenable_io.py view-result --ref <ref> --row 1
tenable_io.py grep --pattern openssl
tenable_io.py ls --path /
```

`--limit` is **required** on both `query` and `get` -- there is no default, and the CLI errors immediately if it's omitted. See Context budget rules below for what to pick. `view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `tenable_io.py query` (`--filter FIELD OP VALUE` repeatable, `--search-type`, `--date-range`, **`--limit` required**, `--output`) | Search vulnerability findings, aggregated by plugin (`GET /workbenches/vulnerabilities`). Returns grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `tenable_io.py get` (`--plugin-id` required, `--date-range`, **`--limit` required**) | Hydrate one plugin's per-asset finding detail (`GET /workbenches/vulnerabilities/{plugin_id}/outputs`) -- one row per affected asset/port occurrence. Always a fresh backend call, not a cache read. |
| `tenable_io.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior `query`/`get` (full entity JSON). Pure cache read. |
| `tenable_io.py grep` | String-search this connector's knowledge-graph catalog -- matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes (populated by `refresh` + the kg-learner describe pass), not the live API; for cross-connector search use `kg_learner.py grep`. |
| `tenable_io.py upload-samples` | Upload sample findings to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (a `--output` artifact), plus the exact filter/date-range combination as `--query` (JSON) used to fetch them, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or when the read-before-write check fails. |
| `tenable_io.py refresh` | Walk every configured Tenable.io connector's catalog (vulnerabilities -> field) and commit it to the KG. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. The only producer path -- `ls` reads from KG nodes exclusively. Run after configuring a new connector; the weekly `kg-schema-refresh` scheduled job invokes this automatically. |
| `tenable_io.py ls --path /<abs-path> --format json` | List this skill's Tenable.io connectors' database schema by UNIX path (Resource -> Field). Field names are discovered empirically from a one-row unfiltered sample of the vulnerabilities resource. JSON output; consumed by the `kg_learner.py ls` aggregator. For human-friendly Markdown/PSV output, call `kg_learner.py ls` instead. |

## Context budget rules

- Default to a small `--limit` (25 is a reasonable starting point for an analyst-facing question). Only raise it on explicit user request, and prefer narrowing `--filter` first. `--limit` is capped at 100 client-side regardless of what's asked for.
- An empty `--filter` with a moderate `--limit` is the right approach for knowledge-graph population (see Uploading samples) -- but for an analyst-facing question, add a filter when the user names a severity, host, or plugin instead of fetching broadly and filtering client-side.
- `--date-range` defaults to 30 days; widen it only when the user's question needs a longer look-back, up to Tenable's own 450-day retention window.
- Render the main finding fields per Rendering pattern below; do not paste full raw JSON for more than a couple of findings inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## Filters

`--filter` is a repeatable `FIELD OP VALUE` triple (Tenable's own `filter.<N>.filter` / `.quality` / `.value` params, joined by `filter.search_type` -- `and` by default, `or` if `--search-type or` is passed and more than one `--filter` is given). Up to 10 filters. An empty `--filter` returns all rows up to `--limit`. https://developer.tenable.com/docs/workbench-filters

Tenable has no native in-list operator; a lookup against multiple values for the same field is sent as multiple `--filter FIELD eq VALUE` triples with `--search-type or`.

Documented, commonly-useful `workbenches/vulnerabilities` filters:

| Field | Operators | Values | Notes |
|---|---|---|---|
| `severity` | `eq`, `neq` | `None`, `Low`, `Medium`, `High`, `Critical` | Static CVSS-based severity, not VPR. |
| `host.target` | `eq`, `neq`, `match`, `nmatch` | string | Hostname/IP the finding was seen on. |
| `ipv4_addresses` | `eq` | string | |
| `host.id` | `eq` | UUID | |
| `asset_assessed` | `eq` | `true`, `false` | |
| `is_licensed` | `eq` | `true`, `false` | |
| `installed_software` | `eq` | CPE string | |
| `plugin.attributes.bid` | `eq` | numeric ID | Bugtraq ID. |
| `plugin.name` | `eq`, `match` | string | Not in the documented common-filters list but accepted per the endpoint's own parameter reference. |

This table is not exhaustive -- Tenable exposes the full, live filter catalog per environment via `GET /filters/workbenches/vulnerabilities` (not wired into this CLI). If a filter returns `400`, don't retry the same field/operator combination -- drop it, fetch unfiltered (or filtered on a supported field from the table above) with a small `--limit`, and filter the result client-side instead.

## Resources

Only the aggregated-by-plugin `workbenches/vulnerabilities` surface is implemented, plus its per-plugin `outputs` detail. Tenable.io's broader surface -- `workbenches/assets` (asset-centric view), `scans`, `plugins`, Web App Scanning, Container Security, and the async `/vulns/export` / `/assets/v2/export` bulk-export job-poll endpoints for pulling more than 5,000 rows at once -- is out of scope for this connector; each would need its own resource entry (or, for the export job-poll pattern, likely its own connector type given the structurally different submit-then-poll call shape) added deliberately, not assumed to work through `query`/`get`.

`workbenches/vulnerabilities` itself is capped by Tenable at 5,000 rows per call and excludes data older than 450 days, independent of this CLI's own smaller `--limit`/context-budget caps.

## VPR vs. severity

Findings carry two independent priority signals -- don't conflate them:

- **`severity`** (0-4: Info/Low/Medium/High/Critical) is static, based on the vulnerability's CVSS score at publication.
- **`vpr_score`** (decimal, 0.1-10.0; Critical 9.0-10.0, High 7.0-8.9, Medium 4.0-6.9, Low 0.1-3.9) is Tenable's dynamic Vulnerability Priority Rating, recalculated daily from real-world exploit/threat signals (exploit code maturity, threat recency and intensity, vulnerability age, affected-product breadth). A finding can be CVSS `High` severity with a low VPR (little real-world exploitation) or vice versa -- when the user asks what to remediate first, prefer `vpr_score` for urgency and cite `severity` as the separate, static classification. https://docs.tenable.com/vulnerability-management/Content/Explore/Findings/RiskMetrics.htm

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded finding must be the byte-identical output of a recent `query` issued from this same container. The round-trip (query -> see results -> choose to upload) is what forces the LLM to be judicious about what is sampled. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** -- capture the `Result reference: <uuid>` line that `query` prints on stdout and pass that UUID. The container-local cache holds the rows that the query just returned.
2. **`--from-file FILE`** -- when the prior `query` was invoked with `--output FILE`, pass that file. Its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

In either form the **exact same filter/date-range combination (passed as `--query`, JSON)** must be passed to `upload-samples` as were used by the originating `query`; mismatched values fail the read-before-write check. (Pass `--query "{}"` when the query used no filter and the default date range.)

The upload cap is a small row count. Tighter filters are the expected pattern -- `--limit 1` is what to reach for first when sampling a single finding. The point is one or a handful of exemplary findings, not bulk data.

`--catalog-path` (required) is the inventory path under which the sample is recorded -- typically `/vulnerabilities/<field>` (e.g. `/vulnerabilities/severity`). The first tier is fixed at `vulnerabilities`. Tiers are created lazily server-side, and the server walks each sample's nested JSON keys into child nodes under the leaf, so the whole finding object shape becomes catalog nodes automatically.

## Rendering pattern

Render a compact card or table per finding -- don't dump raw PSV/JSON as the answer:

- **Plugin** -- `plugin_name` (`plugin_id` for follow-up `get`)
- **Severity / VPR** -- `severity` and `vpr_score` (see VPR vs. severity above)
- **State** -- `vulnerability_state` (e.g. `Active`, `Fixed`, `Resurfaced`)
- **Affected count** -- `count` (findings), `counts_by_severity` if present
- **Plugin family** -- `plugin_family`

For `get` (per-asset detail) rows: **asset** (`fqdn` or `ipv4`), **port/protocol**, **state**, plus the `plugin_output` snippet when it's short enough to be useful inline.

≤5 findings -> inline table. More than that -> show the first several plus a count of remaining matches, and offer to narrow the filter.

## Error handling

- **400** -- the filter uses a field or operator this workbenches endpoint doesn't support, or a malformed `date_range`/pagination value. Drop the offending filter (or switch to a documented-supported field from the table above) rather than retrying the same expression.
- **401** -- the `X-ApiKeys` header is missing or the key pair is invalid/revoked; this is a credential-entry problem, not a permissions problem. Surface that the connector's stored credentials may be invalid -- do not ask the user to paste a key into chat.
- **403** -- authentication succeeded but the account's Tenable user role lacks access to this data (Basic [16] role or higher is required for `workbenches/vulnerabilities`). Surface this as a configuration issue for whoever set up the connector, not something the agent can work around.
- **429** -- Tenable is rate-limiting or a concurrency limit was hit; back off and report the rate limit rather than retrying in a loop. The response's `Retry-After` header (if present) indicates how long to wait. https://developer.tenable.com/docs/rate-limiting
- **5xx** -- upstream Tenable service issue; report the status and retry only if the user asks.
- **Zero rows on a 200** -- a real, correct, empty result, not an error. Report it plainly (the CLI prints `No <command> results (0 rows)`) and suggest loosening the filter or widening `--date-range` rather than retrying the identical call.

## Guardrails

- No write actions exist on this connector -- it is read-only (`GET` only). Do not imply Tenable remediation (accept risk, recast severity, launch/stop a scan) is possible through it.
- Do not treat `severity` alone as the full remediation priority -- check `vpr_score` too (see VPR vs. severity above); a high-severity finding with low real-world exploitation and a lower-severity finding under active exploitation can rank oppositely.
- Do not treat `vulnerability_state: Fixed` findings as still open, and don't treat a finding's mere presence in a query result as proof it's currently unremediated -- check `vulnerability_state` explicitly.
- Do not expose or reconstruct the access key or secret key.
- Do not fabricate finding data when a query returns zero results -- report zero results plainly and suggest loosening the filter or widening the date range.

## Connector base URL

Configure the connector's base URL as the **Tenable Vulnerability Management API root**: `https://cloud.tenable.com`. This is a fixed, single-tenant-cloud host (there is no per-customer subdomain) -- `connector.json` bakes it in as a static `base_url`, not a templated field.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- the skill auto-resolves its bound connector; the server applies the stored `X-ApiKeys` header on every call.

## Output format

`query` and `get` return grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
