---
name: palo-alto-xsoar
description: Drive a Palo Alto Cortex XSOAR 8 connector via the `palo_alto_xsoar.py` CLI. Search incidents by XSOAR's own field:value query syntax (or a small set of structured filters), hydrate one incident by id, resolve custom incident-field names, walk the catalog into the knowledge graph, and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Palo Alto Cortex XSOAR

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/palo_alto_xsoar.py`. Run `scripts/palo_alto_xsoar.py --help` for the subcommand list; `scripts/palo_alto_xsoar.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. This targets **Cortex XSOAR 8's cloud-hosted tenant API** (`https://api-<fqdn>/xsoar/public/v1`) specifically -- the on-prem 6.x/8.x XSOAR API surface is a different product line and is out of scope for this connector. Only the `incidents` resource is implemented and this connector is **read-only** -- there are no create/update/close/delete actions available through it, even though the vendor API itself supports them.

**None of this connector's claims have been live-verified against a real XSOAR tenant** -- this is a docs-only build (see Error handling and Query syntax below for the specific gaps this leaves).

## Auth recap

The connector stores three fields -- `api_host`, `key_id`, `api_key` -- and crogld attaches two headers automatically on every call: `x-xdr-auth-id: <key_id>` and `Authorization: <api_key>`. Never ask the user for these values during analysis, never print them, and never construct XSOAR auth headers manually.

This is the vendor's **Standard**-type API key. Cortex XSOAR 8 also offers an **Advanced**-type key whose `Authorization` header is a per-request `SHA256(api_key + nonce + timestamp)` hash (with accompanying `x-xdr-nonce`/`x-xdr-timestamp` headers) to prevent replay attacks -- this connector does not support Advanced keys, since a static header binding cannot compute a value that changes on every request. When provisioning, generate a **Standard** key.

## Tool call shape

```bash
palo_alto_xsoar.py query --query "-status:closed -category:job" --limit 10
palo_alto_xsoar.py query --type Phishing --from-date "2024-01-01T00:00:00Z" --limit 25
palo_alto_xsoar.py get-incident --id 162669
palo_alto_xsoar.py list-incident-fields
palo_alto_xsoar.py view-result --ref <ref> --row 1
palo_alto_xsoar.py grep --pattern owner
palo_alto_xsoar.py ls --path /
```

`view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `palo_alto_xsoar.py query` (`--query`, `--category`, `--type`, `--from-date`, `--to-date`, `--sort-field`, `--sort-desc/--sort-asc`, `--limit`, `--output`) | Search incidents (`POST /incidents/search`). Returns grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `palo_alto_xsoar.py get-incident --id <id>` | Hydrate one incident by numeric id (`GET /incident/load/{id}`) -- full JSON detail card. |
| `palo_alto_xsoar.py list-incident-fields` | List every incident field definition in the tenant (`GET /incidentfields`), including custom fields -- use this to resolve a human field name (or a custom field's `cliName`) before using it in `--query` or reading it out of `CustomFields`. |
| `palo_alto_xsoar.py grep` | String-search this connector's knowledge-graph catalog -- matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes (populated by `refresh` + the kg-learner describe pass), not the live API; for cross-connector search use `kg_learner.py grep`. |
| `palo_alto_xsoar.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full incident JSON). Pure cache read. |
| `palo_alto_xsoar.py upload-samples` | Upload sample incidents to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (an `--output` artifact), plus the exact `--query` value used to fetch them, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or when the read-before-write check fails. |
| `palo_alto_xsoar.py refresh` | Walk the incidents field catalog (`incidents -> Field`, from `GET /incidentfields`) and commit it to the KG via `commit_walk_tier`. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. Prunes stale entries. The only producer path -- `ls` reads from KG nodes exclusively. Run after configuring a new connector; the weekly `kg-schema-refresh` scheduled job invokes this automatically. |
| `palo_alto_xsoar.py ls --path /<abs-path> --format json` | List this skill's XSOAR connectors' database schema by UNIX path (`incidents -> Field`). Field names come from the tenant's real `/incidentfields` schema endpoint, not a sampled row. JSON output; consumed by the `kg_learner.py ls` aggregator. |

## Context budget rules

- Default to a small `--limit` (10 is a reasonable starting point for an analyst-facing question). Only raise it on explicit user request, and prefer narrowing `--query` first.
- Render the main incident fields per Rendering pattern below; do not paste full raw JSON for more than a couple of incidents inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## Query syntax

Cortex XSOAR's incident search has two independent surfaces, and **the vendor docs state that setting `filter.query` causes the API to ignore every other filter field** -- so this CLI treats `--query` and the structured flags (`--category`/`--type`/`--from-date`/`--to-date`) as mutually exclusive: passing `--query` drops the others.

**Free-text `--query`** (the same syntax as the incident search bar in the XSOAR UI): `field:value` pairs, colon is equality, a leading `-` excludes, multiple terms combine as AND. Confirmed example from the vendor's own Administrator Guide:

```
-status:closed -category:job
```

Documented filterable fields for this syntax: `status`, `category`, `severity`, `type`, plus `created` for date-range terms (the admin-guide walkthrough adds a date range via a UI dropdown rather than quoting the exact in-query date syntax, so a `created:` term's exact operator/format is **not confirmed** -- if a `created:` term is rejected, drop it and use the structured `--from-date`/`--to-date` flags instead, which map to the documented `filter.fromDate`/`filter.toDate` JSON fields).

**`status` and `severity` are numeric fields in the API** (`filter.status: [number]`, `level: [number]`), but no vendor page found during this build states the number-to-label mapping (e.g. which integer is "Closed" vs "Archived"). Rather than guess, use the string-based `--query "-status:closed"` form shown above (confirmed working via the search-bar UI) instead of a raw numeric `--status` flag -- this connector deliberately does not expose one.

**Structured flags** (used only when `--query` is empty): `--category` and `--type` map directly to the documented `filter.category: [string]` / `filter.type: [string]` array fields; `--from-date`/`--to-date` map to `filter.fromDate`/`filter.toDate` (ISO 8601, UTC). `--sort-field`/`--sort-desc` map to `filter.sort`.

Run `list-incident-fields` to see the full set of standard and custom fields on this tenant, including each field's `cliName` -- the identifier used in query terms and in the `CustomFields` object on a hydrated incident.

## Resources

Only `incidents` is implemented. The broader XSOAR surface (indicators, investigations/workplans, playbooks, integrations, content packs, users/roles, reports/dashboards, audit logs) is out of scope for this connector -- each would need its own auth-scope and call-shape review before being added, the same way this connector itself was scoped to one coherent resource family rather than the whole platform.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded incident must be the byte-identical output of a recent `query` issued from this same container. The round-trip (query -> see results -> choose to upload) is what forces the LLM to be judicious about what is sampled. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** -- capture the `Result reference: <uuid>` line that `query` prints on stdout and pass that UUID. The container-local cache holds the incidents that the query just returned.
2. **`--from-file FILE`** -- when the prior `query` was invoked with `--output FILE`, pass that file. Its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

In either form the **exact same `--query` value** must be passed to `upload-samples` as was passed to the originating `query`; a mismatched value fails the read-before-write check. (Pass `--query ""` when the originating query used only structured flags, since those are recorded as the effective query text.)

The upload cap is a small row count -- tighter queries are the expected pattern; `--limit 1` is what to reach for first when sampling a single incident. The point is one or a handful of exemplary incidents per leaf, not bulk data.

`--catalog-path` (required) is the inventory path under which the sample is recorded -- for this connector, `/incidents/<field>` (e.g. `/incidents/severity`). Tiers are created lazily server-side, and the server walks each sample's nested JSON keys into child nodes under the leaf, so the whole incident object shape becomes catalog nodes automatically.

## Edge-connectable fields

When picking source or target nodes for `kg_learner.py create-edge`, use **scalar identity fields** from a sampled incident. The server's inner-join check intersects bare JSON string leaves between two `data_samples`; an incident exposes several:

- Incident identity: `id`, `numericId`, `name`
- Cross-connector join targets: any IOC-shaped value surfaced under `CustomFields` on this tenant (hostnames, IPs, hashes, usernames) -- these are what would line up with a CrowdStrike host, a QRadar offense asset, or a ServiceNow CI on another connector. Which `CustomFields` keys exist is tenant-specific; run `list-incident-fields` first to find them.

**Do not edge against free prose** like `details` or investigation notes -- the inner-join requires whole-leaf equality, so an identifier embedded in prose won't match a bare leaf from another connector.

## Rendering pattern

Render a compact card or table per incident -- don't dump raw PSV/JSON as the answer:

- **Name** -- `name`
- **Status** -- `status` (numeric; pair with the `--query` string form when reporting to a human, since the label mapping isn't confirmed -- see Query syntax)
- **Severity** -- `severity`
- **Type / category** -- `type`, `category`
- **Owner** -- `owner`
- **Created / closed** -- `created`, `closed`
- **Incident id** -- `id` (needed for a follow-up `get-incident` or `view-result`)

≤5 incidents -> inline table. More than that -> show the first several plus a count of remaining matches, and offer to narrow the query.

## Error handling

- **401/403** -- the connector's stored credential was rejected, or the API key lacks a required role/permission. Surface this as a configuration issue for whoever set up the connector -- do not ask the user to paste a key into chat, and do not retry the identical call expecting it to self-heal (this connector's Standard key has no token-refresh step to wait out).
- **404 on `get-incident`** -- the id doesn't exist on this tenant (or was deleted); report it as not found rather than retrying.
- **400 on `query`** -- most likely a malformed `--query` term (see Query syntax) or an unsupported field name -- run `list-incident-fields` to confirm the real field/`cliName` before retrying, rather than guessing at another spelling.
- **A `query` request that returns `200` with `total: 0`** -- a valid query with no matching incidents, not a failure. Report it plainly and suggest loosening the query rather than retrying identically.
- **"not supported in multi-tenant environments"** -- the vendor docs flag `/incidents/search` as unavailable on multi-tenant XSOAR deployments; if this tenant is multi-tenant, `query` will fail structurally regardless of the query itself. This has not been confirmed against a live tenant.
- **5xx** -- upstream XSOAR service issue; report the status and retry only if the user asks.

## Guardrails

- No write actions exist on this connector -- it is read-only. Do not imply incident creation, status changes, closing, or playbook execution are possible through it, even though the underlying XSOAR API supports all of these.
- Do not fabricate incident data when a query returns zero results -- report zero results plainly and suggest loosening the query.
- Do not present a raw numeric `status`/`severity` value as a confirmed label (e.g. "status 2 means Closed") -- that mapping is not vendor-confirmed by this build; prefer the string-based `--query` form instead.
- Do not expose or reconstruct the `key_id` or `api_key`.

## Connector base URL

Configure the connector's base URL as `https://<api_host>/xsoar/public/v1`, where `api_host` is the tenant-specific FQDN shown when generating an API key (e.g. `api-abc123.paloaltonetworks.com`). See the provisioning guide for exactly where to find it.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- the skill auto-resolves its bound connector; the server applies the `x-xdr-auth-id`/`Authorization` headers stored on that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
