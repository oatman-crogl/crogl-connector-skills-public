---
name: sophos-central
description: "Drive a Sophos Central (Fusion) connector via the sophos_central.py CLI. List alerts, cases, and managed endpoints from the tenant-scoped Sophos Central REST API, drill into a result, hydrate an entity by id, walk the catalog into the knowledge graph, and emit samples for edge learning. Talks to a configured connector (APIProxy), resolved automatically. Auth (OAuth2 Bearer) and the X-Tenant-ID header are applied server-side; no upstream secrets in this container."
status: field-tested
version: "0.1.3"
---

# Sophos Central (Fusion)

![status](https://img.shields.io/badge/status-field--tested-yellow) ![version](https://img.shields.io/badge/version-0.1.3-blue)

One CLI: `scripts/sophos_central.py`. Run `scripts/sophos_central.py --help` for the subcommand list; `scripts/sophos_central.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Three resources are implemented — `alerts`, `cases`, and `endpoints` — via the tenant-scoped Sophos Central REST API. This connector is **read-only** (GET only); there are no write/remediation actions (isolate endpoint, close case, run scan) wired into it.

> **Product note.** "Sophos Central" and "Sophos Fusion" are the same platform — Fusion is the console rebrand built on the Sophos Central API (`api.central.sophos.com`). This connector is **not** Secureworks Taegis — that is a separate platform with a separate GraphQL API, separate identity system, and separate credentials (a Sophos Central credential does not authenticate to Taegis). Use the `taegis` connector for a Taegis tenant.

## Changelog

- **0.1.3** — `--limit` on `query` is now code-enforced as `click.IntRange(1, 100)` (CONTRIBUTING.md "Every list/query command has a code-enforced result-size ceiling") -- previously a bare `type=int` with no ceiling. `_search`'s cursor-pagination loop now clamps `limit` to the same 100-row hard cap before it starts (defense in depth, since `_search` is also reachable from the shared `sample`/`lookup` commands). Per-page `pageSize` was already explicitly capped; this closes the gap in how many pages a request could walk. No call-shape change for a request already within the cap.
- **0.1.2** — Empty-result wording standardized to match this repo's canonical phrasing (`qradar`/`jira-datacenter`): `No <resource> results (0 rows)` instead of the earlier `No <resource> found (0 results)`. Output-only; no call-shape, query, or credential changes.
- **0.1.1** — Field-test results folded in (docs only, no behavior change). All three resources (`alerts`, `cases`, `endpoints`) and `get`-by-id exercised against a live Sophos Central tenant with **real data**; field names and PSV rendering confirmed against real records; multi-page pagination confirmed live (real `pages.nextKey`→`pageFromKey` walk); the read-only guardrail confirmed in practice; and error responses (401/400/404) verified against live API responses. See "Field-test status".
- **0.1.0** — Initial release. Alerts (Common API), cases (Cases API), and endpoints (Endpoint API) over the tenant-scoped Central REST API, with the standard query / view-result / get / refresh / ls / grep / upload-samples surface. **Field-tested** against a live Sophos Central tenant (US region): OAuth2 mint → tenant-scoped `GET`s with `X-Tenant-ID` return `200` end-to-end through the product; empty result sets print an explicit `No <resource> found (0 results)` line. SIEM events and the async Detections query-job API are deferred (see "Deferred surfaces").

## Field-test status (read before relying on output)

Field-tested end-to-end against a live Sophos Central tenant through Crogl, with **real data** (one enrolled endpoint, one open alert, one case):

- **`endpoints`** — `query` returned the enrolled device and `get --resource endpoints --id <id>` hydrated it; rendered fields (`hostname`, `health`, `os`, `type`, `ipv4Addresses`, `lastSeenAt`, …) match the real record.
- **`alerts`** — `query` returned a live high-severity alert; the rendered fields (`severity`, `category`, `product`, `description`, endpoint, `raisedAt`, status) match the real record.
- **`cases`** — `query` returned a live case; rendered fields (`id`, `name`, `status`, `severity`, `assignee`, `createdAt`) match the real record.
- **Auth + routing** — OAuth2 client-credentials mint → tenant-scoped `GET`s to the data-region host with the server-injected `X-Tenant-ID` header return `200`.
- **Read-only guardrail** — confirmed in practice: asked to comment on a case, the agent correctly reported that no write/`add-comment` subcommand exists and did not attempt a write.
- **Error handling** — live-verified against real responses: bad secret/token → `401`, bad query parameter → `400` (`"Parameter … has invalid format"`), unknown id → `404` (`"Resource not found"`), wrong host/path → `404` (`"Unable to identify proxy for host"`). Credentials are **not** validated at connector-create (crogld stores them without a probe), so an invalid `client_id`/secret first surfaces as a `401` on the initial query — not at setup.

- **Multi-page pagination** — confirmed live: with the page size forced small over two alerts, the connector followed Sophos's real `pages.nextKey` (base64 offset cursors) back as `pageFromKey` across pages and terminated correctly. Also unit-tested against the real `_search` (threads `pageFromKey`, stops on absent `nextKey`, honors `--limit`). Note: Sophos returns a `nextKey` even on the final data page, so a complete walk ends with one trailing request whose `items` is empty — the connector stops on that.

Behavioral note — a freshly-enrolled endpoint reports `health: unknown` (threats/services `unknown`) for a short window until the agent's first check-in; read that as "not yet reported," not "unhealthy," when interpreting endpoint results.

## Auth recap

The connector stores four fields — `client_id`, `client_secret`, `api_host`, `tenant_id`. crogld exchanges the client credentials server-side for a bearer token via `https://id.sophos.com/api/v2/oauth2/token` (OAuth2 client-credentials, `scope=token`), attaches `Authorization: Bearer` automatically, and attaches the required `X-Tenant-ID: <tenant_id>` header on every call. Tokens are re-minted automatically. Never ask the user for the client ID, secret, or tenant ID during analysis, never print them, and never construct auth headers manually.

## Tenant / region setup

Sophos Central is tenant-scoped and region-partitioned. Two connect-form values come from one setup call — `GET https://api.central.sophos.com/whoami/v1` with a bearer token:

- `api_host` — the tenant's **data-region host** from `apiHosts.dataRegion` (e.g. `api-us01.central.sophos.com`), no scheme. All data calls must go here — the global host and other regions won't serve this tenant's data.
- `tenant_id` — the `id` field from whoami. Required as the `X-Tenant-ID` header.

The provisioning guide has the exact `whoami` call. (The CLI can't self-discover these — `whoami` is only served on the global host, and the connector is pinned to the data-region host.)

## Tool call shape

```bash
sophos_central.py query --resource alerts --filter "severity=high" --limit 25
sophos_central.py query --resource endpoints --filter "healthStatus=bad" --limit 25
sophos_central.py query --resource cases --limit 25
sophos_central.py get --resource endpoints --id <endpoint-id>
sophos_central.py view-result --ref <ref> --row 1
sophos_central.py grep --pattern hostname
sophos_central.py ls --path /
```

`--limit` is **required** on `query` -- there is no default, and the CLI errors immediately if it's omitted. It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped. `view-result` requires `--ref` and `--row` (1-based within the cached result); `--result-set` defaults to `0`. `--resource` on `query`/`get` defaults to `alerts` — pass `--resource endpoints` / `--resource cases` explicitly.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `sophos_central.py query` (`--resource`, `--filter`, **`--limit` required, hard cap 100**, `--output`) | List a resource through the connector. Cursor-paginates (`pages.nextKey` → `pageFromKey`) up to `--limit`; returns grouped PSV with a `ref` UUID. `--output FILE` also writes rows in the verifiable upload-samples format. |
| `sophos_central.py get` (`--resource` defaults to `alerts`, `--id` required) | Hydrate one entity by id; no prior query ref needed. `404` → reported as not found. |
| `sophos_central.py view-result` (`--ref`, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full entity JSON). Pure cache read. |
| `sophos_central.py grep` | String-search this connector's KG catalog — `--pattern` against schema-element names, summaries, and whole tags. Literal substring by default; `--regex` / `--fuzzy`. Reads persisted KG, not the live API. |
| `sophos_central.py upload-samples` | Upload sample entities to the KG. `--ref UUID` or `--from-file FILE`, plus the exact `--query` (the `--filter` used) and `--catalog-path`. Refuses over a small row cap or on a failed read-before-write check. |
| `sophos_central.py refresh` | Walk each connector's catalog (resource → field) into the KG. Resumable; `--max-age` (default `5d`); `--force`. The only producer path — `ls` reads KG only. |
| `sophos_central.py ls --path /<abs-path> --format json` | List this skill's Sophos connectors' schema by UNIX path (Resource → Field). Fields discovered from a one-row sample. |

## Query model

`--filter` is a **raw Sophos query-parameter string** (`k=v&k2=v2`) appended to the collection request — Sophos filtering is per-endpoint query parameters, not a unified grammar. Empty `--filter` returns all rows up to `--limit`. Examples (confirm exact parameter names/values per the Sophos API reference for the tenant's version):

- **alerts** (`/common/v1/alerts`): commonly `product`, `category`, `severity` (e.g. `low`/`medium`/`high`), `from`/`to` (ISO-8601 time window), `groupKey`.
- **endpoints** (`/endpoint/v1/endpoints`): commonly `healthStatus` (e.g. `good`/`suspicious`/`bad`), `search` (hostname/IP substring), `lastSeenBefore`/`lastSeenAfter`, `type`.
- **cases** (`/cases/v1/cases`): commonly status/severity/assignee filters.

Quote the whole `--filter` in the shell. If a parameter returns a 4xx, drop it (or correct it against the API reference) rather than retrying.

## Context budget rules

- `--limit` is mandatory; default to a small value (25). Raise only on explicit request; prefer tightening `--filter` first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, so there's no "just set --limit very high" option for bulk population; run multiple narrower `query`s instead.
- Empty `--filter` still returns a broader unfiltered slice for knowledge-graph population, but it's bounded by the same 100-row hard cap as everything else -- for analyst questions, add a filter when the user names a severity, health status, or host.
- Render the main fields (below); don't paste full raw JSON for more than a couple of entities. Drop raw PSV/JSON from reasoning after rendering; reuse the existing `ref` via `view-result` instead of re-running an identical query.

## Resources

- `alerts` (`/common/v1/alerts`) — cross-product alerts: severity, category, product, description, timestamps, and the associated endpoint/managedAgent.
- `cases` (`/cases/v1/cases`) — analyst cases (the Fusion investigation work-item): status, severity, assignee, created/updated times.
- `endpoints` (`/endpoint/v1/endpoints`) — managed device inventory: hostname, health, os, ipv4/ipv6 addresses, type, last-seen, tamper-protection state.

All three return full entities at list time, paginated by the `items` + `pages.nextKey` cursor. Each page request sends an explicit `pageSize` (Sophos's own per-request ceiling is 500), and the overall walk stops once the (clamped) `--limit` — hard-capped at 100 — is reached, not just when Sophos runs out of pages.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded entity must be the byte-identical output of a recent `query` from this same container. Use `--ref UUID` (the `Result reference` line `query` prints) or `--from-file FILE` (a `query --output` artifact, sha256-checked). Pass the **exact `--filter` value as `--query`** used for the originating query (empty string if none). The upload cap is small — tighter filters and `--limit 1` are the pattern. `--catalog-path` (required) is `/<resource>/<field>` (e.g. `/endpoints/hostname`); tiers are created lazily server-side and nested JSON keys walked into child nodes.

## Edge-connectable fields

For `kg_learner.py create-edge`, use **scalar identity fields** (the server inner-joins bare JSON string leaves):

- Endpoint (from `endpoints`): `id`, `hostname`, `ipv4Addresses`, `macAddresses` — the device join keys, and what line up with a CrowdStrike host, Defender machine, or Sentinel entity on other connectors.
- Alert → device (from `alerts`): the alert's `managedAgent`/endpoint id links back to `endpoints`.

Endpoints are the connective tissue — learning their identity fields is what lets an alert or case resolve to a real device across connectors. Don't edge against free prose (an alert `description`, a case summary) — the inner-join needs whole-leaf equality.

## Rendering pattern

Render a compact table — don't dump raw PSV/JSON:

- **Alerts** — severity · category · product · description · raisedAt · endpoint.
- **Cases** — id · severity · status · name · assignee · createdAt.
- **Endpoints** — hostname · health · os · type · ipv4Addresses · lastSeenAt.

≤5 rows → inline table. More → show the first several plus a count of remaining matches, and offer to narrow the filter.

## Error handling

- **400** — a bad query parameter or value. Drop/correct the `--filter` against the Sophos API reference rather than retrying the same thing.
- **401** — bearer token invalid/expired; self-heals via the connector's OAuth flow. If it persists, the stored client credentials may be invalid/revoked — surface as a connector-config issue; never ask the user to paste a token into chat. Note: credentials aren't verified when the connector is created, so a wrong `client_id`/secret shows up here (first query), not at setup — a `401` right after configuring a new connector usually means a mistyped credential.
- **403** — the API credential's **role** lacks access to that resource (Sophos API credentials are role-scoped). Surface as a configuration issue for whoever created the credential.
- **404** — on `get`, the id doesn't exist (reported as not found). On a collection path, a wrong `api_host`/tenant routing (the gateway returns `Unable to identify proxy for host` when the path or region host is wrong) — check the connector's `api_host`.
- **429** — Sophos rate-limits; back off and report rather than retrying in a loop.
- **5xx** — upstream service issue; report the status and retry only if the user asks.

## Guardrails

- Read-only. No write/remediation actions exist here — do not imply endpoint isolation, scans, or case closure are possible through it.
- Do not expose or reconstruct the client ID, client secret, or tenant ID.
- Don't fabricate data when a query returns zero results — report zero plainly (a fresh tenant with no enrolled endpoints legitimately returns empty) and suggest loosening the filter.
- This is Sophos Central, not Secureworks Taegis — don't conflate the two platforms or their data in analysis.

## Deferred surfaces (not in this version)

- **SIEM events** (`/siem/v1/events`) — requires `limit ≥ 200` and uses a distinct pagination style; deferred to keep v1's pagination uniform.
- **Detections (XDR)** (`/detections/v1/...`) — an asynchronous query-job API (submit a query, poll, fetch by id), not a simple GET. Deferred until it can be modeled on the poll pattern and exercised against a tenant with real detections. This is the highest-value follow-up.

## Connector base URL

Configure the connector's base URL as the tenant's **data-region host** (`https://<api_host>`, e.g. `https://api-us01.central.sophos.com`). The CLI sends absolute API paths (`/common/v1/alerts`, `/endpoint/v1/endpoints`, `/cases/v1/cases`).

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets — the skill auto-resolves its bound connector; the server applies the OAuth Bearer and `X-Tenant-ID` stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
