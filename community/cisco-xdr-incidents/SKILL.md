---
name: cisco-xdr-incidents
description: Drive a Cisco XDR Incidents & Investigations (Conure v2) connector via the `cisco_xdr_incidents.py` CLI. Read-only search over incidents plus per-incident and per-investigation detail hydration (summary, entities, observables, verdicts, events, MITRE, graph). Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Cisco XDR Incidents & Investigations

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

Connector manifest: [connector.json](./connector.json).

One CLI: [scripts/cisco_xdr_incidents.py](./scripts/cisco_xdr_incidents.py). Run `scripts/cisco_xdr_incidents.py --help` for the subcommand list; `scripts/cisco_xdr_incidents.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. This connector is **read-only** -- there are no write, mutation, or response actions exposed here.

**None of this connector's claims have been live-verified against a real Cisco XDR tenant.** It was built by downloading and reading the live Conure v2 OpenAPI spec (`https://conure.us.security.cisco.com/swagger.json`) directly, plus Cisco's published DevNet/XDR docs -- not from vendor doc-summary tools alone, and not from training-data memory of "how this API probably works." Every endpoint path, parameter, enum, and response field named below traces to that spec or a fetched doc page. Treat all behavior notes as docs-grounded rather than tenant-proven, and treat anything explicitly marked "not vendor-confirmed" below as an even weaker claim than the rest -- the underlying OpenAPI spec is silent on it, not just untested.

## Scope

This connector implements a deliberately narrow, coherent read-only surface, scoped to the `v1`/`v2`-tagged Incident and Investigation Data endpoints of the Conure API:

- **Incident search** via `GET /v2/incident/search`
- **Incident count** via `GET /v2/incident/search/count` -- same filter set as search, no rows fetched
- **Incident get** via `GET /v2/incident/{incident-id}`
- **Incident detail views** via `GET /v2/incident/{incident-id}/<view>` -- overview, summary, entities, observables, verdicts, events, mitre, targets, status, indicators, primary-investigation, graph, errors, export, linked-casebooks, linked-incidents, report
- **Investigation detail views** via `GET /v2/investigation/{investigation-id}/<view>` -- overview, summary, entities, observables, verdicts, events, indicators, targets, status, graph

`linked-casebooks` reads which casebooks reference this incident (a `GET`, no casebook-management scope or write access implied) -- it is a cross-reference view, not part of the Casebook CRUD surface excluded below.

Deliberately **out of scope**, left as candidate future connector types (the same call-shape/trust-boundary split as Purview DLP/Audit/eDiscovery):

- **Casebook management** (`/v2/casebook/*`) -- a separate case-management resource with its own CRUD lifecycle, not incident data.
- **Incident/investigation write & creation** (`POST /v2/incident`, `PATCH`/`PUT`/`DELETE /v2/incident/{id}`, `POST .../actions-taken`, `POST .../status`, `POST /v2/investigation`, `.../bundle`, `.../snapshot`, `.../save-as`, `.../copy`, `.../task`, observable add/remove, `POST .../report/{section-id}` and `.../regenerate`) -- write actions need elevated scopes and explicit user-approval guardrails this connector doesn't implement. `incident-detail --view report` (read-only, all sections in one call) is in scope; the single-section `GET .../report/{section-id}` variant is skipped since `report` alone already returns every section.
- **Report/metrics family** (`/v2/report/*`) -- aggregate dashboards (mean-time-to-contain, top-targeted-assets, etc.), a different call shape (tenant-wide stats, not incident records) from everything else here.
- **v3 entity-graph routes** (`/v3/incident/{id}/storyboard`, `/v3/incident/{id}/entity/{entity-id}`, `/v3/incident/{id}/graph` POST, and their investigation equivalents) -- a distinct API generation (compacted-entity graph model) layered on top of the v1/v2 surface this connector uses; a coherent follow-up scope rather than folded in here.
- **`v1/incident-summary/*` and `v1/incident/{id}/assets`** -- superseded by the v2 equivalents this connector uses (`v2/incident/{id}/summary`, `v2/incident/{id}/targets`).

Cisco XDR has **no standalone "search/list investigations" endpoint** -- investigations are reachable only by id, most naturally discovered via `incident-detail --view primary-investigation`. There is also **no standalone "get investigation"** base-record endpoint; `investigation-detail --view overview` is the closest equivalent.

## Auth recap

The connector stores four fields -- `api_host`, `token_host`, `client_id`, `client_secret` -- and crogld mints a short-lived OAuth2 `client_credentials` Bearer token server-side (`POST https://<token_host>/iroh/oauth2/token`, HTTP Basic `client_id:client_secret`) and attaches it as `Authorization: Bearer <token>` on every call to `https://<api_host>`. Never ask the user for these values during analysis, never print them, and never construct the token or headers manually.

**Two separate host fields, not one "region" field.** Cisco's own hostname convention is asymmetric: the Conure API host always carries a region prefix (`conure.us.security.cisco.com`, `.eu.`, `.apjc.`), but the default/US OAuth2 token host has **no** region prefix (`visibility.amp.cisco.com`, vs. `visibility.eu.amp.cisco.com` / `visibility.apjc.amp.cisco.com` for the other two regions). `connector.json`'s binding-host policy requires each templated host to be one single literal field value (no `{{field}}.fixed-suffix` mixing), so a computed `conure.{{region}}.security.cisco.com` isn't expressible -- both hosts are instead entered as full literal strings, matching the precedent set by `jira-datacenter`'s fully-templated `{{host}}`.

Required OAuth2 scopes are **best-guess, not vendor-confirmed**: the live Conure OpenAPI spec declares every operation's security requirement with an empty scope list (`"security": [{"oauth2": []}, ...]`), so it does not state which of Cisco XDR's platform-level scopes (`global-intel:read`, `private-intel:read`, `event:read`, `enrich:read`, `asset`, etc.) each endpoint actually needs. `connector.json` requests `global-intel:read private-intel:read event:read enrich:read asset` based on those scopes' own descriptions in the platform's OAuth2 config ("Needed to fetch global incident data and investigations" / "Needed to get investigation data" / "Needed to get full report information" / "Needed for investigations" / "Needed during running investigations" respectively) -- verify against a live 403 during Phase 6a testing before trusting this list.

## Tool call shape

```bash
cisco_xdr_incidents.py query-incidents --status "Open: Investigating" --limit 25
cisco_xdr_incidents.py query-incidents --high-impact --from 2026-08-01T00:00:00Z --limit 10
cisco_xdr_incidents.py query-incidents --query "ransomware" --search-fields title,description --limit 10
cisco_xdr_incidents.py count-incidents --status "Open: Investigating"
cisco_xdr_incidents.py get-incident --id 7889a175-f43c-42ad-baac-ff55497c1730
cisco_xdr_incidents.py incident-detail --id 7889a175-f43c-42ad-baac-ff55497c1730 --view summary
cisco_xdr_incidents.py incident-detail --id 7889a175-f43c-42ad-baac-ff55497c1730 --view report
cisco_xdr_incidents.py incident-detail --id 7889a175-f43c-42ad-baac-ff55497c1730 --view primary-investigation
cisco_xdr_incidents.py investigation-detail --id <investigation-id> --view overview
cisco_xdr_incidents.py view-result --ref <ref> --row 1
cisco_xdr_incidents.py grep --pattern severity
cisco_xdr_incidents.py ls --path /
```

`view-result` requires both `--ref` and `--row` (a 1-based row number within the cached result); `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `cisco_xdr_incidents.py query-incidents` (see flags below, `--offset`, `--limit`, `--output`) | Search incidents from `GET /v2/incident/search`. |
| `cisco_xdr_incidents.py count-incidents` (same filter flags, no pagination/sort) | Count matching incidents from `GET /v2/incident/search/count` without fetching rows -- cheaper way to gauge result size before deciding whether to narrow a search. |
| `cisco_xdr_incidents.py get-incident --id <id>` | Hydrate one incident's base record from `GET /v2/incident/{id}`. |
| `cisco_xdr_incidents.py incident-detail --id <id> --view <view>` | Hydrate one incident detail view (see Resources table). |
| `cisco_xdr_incidents.py investigation-detail --id <id> --view <view>` | Hydrate one investigation detail view (see Resources table). No standalone "get investigation" -- use `--view overview`. |
| `cisco_xdr_incidents.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior `query-incidents` call (full JSON). Pure cache read. |
| `cisco_xdr_incidents.py grep` | String-search this connector's knowledge-graph catalog. Reads persisted KG nodes, not the live API. |
| `cisco_xdr_incidents.py refresh` | Walk the catalog (`incidents` -> field) and commit it to the KG from one sampled search row. |
| `cisco_xdr_incidents.py ls --path /<abs-path> --format json` | List this connector's schema by UNIX path. |
| `cisco_xdr_incidents.py sample` / `lookup` / `upload-samples` | KG edge-learning row fetch and provenance-anchored sample upload (incidents only -- see Resources table). |

## Query/filter syntax

### `query-incidents` flags

Every flag below maps 1:1 to a documented query parameter of `GET /v2/incident/search` in the live Conure v2 OpenAPI spec (`https://conure.us.security.cisco.com/swagger.json`) -- Cisco's own prose docs page for this API does not enumerate these at all. Most of these parameters carry an **empty description in the spec itself**, so combination semantics (does `--categories foo,bar` OR-match, or does Conure expect one call per value?) are genuinely undocumented. Pass one value per flag unless a live test proves multi-value behavior.

| Flag | Conure param | Notes |
|---|---|---|
| `--id` | `id` | Exact incident id. |
| `--status` | `status` | One of 24 documented `IncidentStatusType` enum values, e.g. `"Open: Investigating"`, `"Closed: Confirmed Threat"`. |
| `--tlp` | `tlp` | `amber` \| `green` \| `red` \| `white`. |
| `--from` / `--to` | `from` / `to` | ISO 8601 date-time bounds. |
| `--assignees` | `assignees` | Plain string match; not vendor-confirmed as multi-value. |
| `--categories` | `categories` | Plain string match; not vendor-confirmed as multi-value. |
| `--confidence` | `confidence` | Plain string match. |
| `--detection-sources` | `detection_sources` | Plain string match. |
| `--discovery-method` | `discovery_method` | Plain string match. |
| `--high-impact` / `--no-high-impact` | `high_impact` | Boolean. |
| `--intended-effect` | `intended_effect` | Plain string match. |
| `--promotion-method` | `promotion_method` | Plain string match. |
| `--query` | `query` | Free-text; grammar undocumented by the vendor -- treat as opaque. |
| `--simple-query` | `simple_query` | Free-text, vendor docs say only "Query String with simple query format". |
| `--search-fields` | `search_fields` | Comma-separated; documented enum: `id`, `short_description`, `title`, `source`, `description`. |
| `--source` | `source` | Plain string match. |
| `--language` | `language` | Plain string match. |
| `--sort-by` | `sort_by` | Comma-separated field list, each optionally suffixed `:asc`/`:desc`, e.g. `timestamp:desc`. Documented sortable fields: `id`, `language`, `revision`, `schema_version`, `source`, `source_uri`, `timestamp`, `title`, `tlp`. |
| `--sort-order` | `sort_order` | `asc` \| `desc`. The spec exposes this as a **second, separate** sort-direction param alongside `sort_by`'s own inline `:asc`/`:desc` suffix; how the two interact when both are given is not documented -- prefer setting direction via `--sort-by`'s suffix alone. |

## Context budget rules

- Default `--limit` is 25. Only raise it on explicit user request, and prefer narrowing filters first.
- This connector enforces a client-side cap of **100** rows on `query-incidents`; the vendor does not document a hard server-side maximum, so this cap is a guardrail, not a documented vendor limit.
- Render the main fields per Rendering pattern below; don't paste full raw JSON for more than a couple of rows inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical query already in context -- use `view-result` on the existing `ref` instead.
- Detail views (`incident-detail`, `investigation-detail`) can return large nested JSON (especially `summary`, `events`, `graph`, `export`, and `linked-casebooks`) -- summarize counts/key fields in the answer rather than echoing the full payload back to the user. `export` in particular duplicates most of `summary`'s content plus more, so prefer `summary` first and only reach for `export` when the user explicitly wants the full dump.
- Use `count-incidents` before a broad `query-incidents` call when the filter is loose (e.g. no `--status`/date bounds) -- it's cheaper than fetching and discarding rows just to learn there are hundreds of matches.

## Resources

| Resource | View | Endpoint | Shape | In `refresh`'s KG walk? |
|---|---|---|---|---|
| `incidents` | (search) | `GET /v2/incident/search` | array of incident objects | Yes |
| `incidents` | (count) | `GET /v2/incident/search/count` | plain integer | -- |
| `incidents` | (get) | `GET /v2/incident/{id}` | one incident object (same shape as a search row) | -- |
| `incidents` | `overview` | `.../overview` | object: id, title, description, owner, investigated_observables, saved/saved_at | No -- detail-only |
| `incidents` | `summary` | `.../summary` | object: event_count, first/last_seen_event, sources, plus nested `indicators`/`observables`/`targets` (each `{total_count, data[]}`) | No |
| `incidents` | `entities` | `.../entities` | array of entity objects (type, observables[], disposition, sightings) | No |
| `incidents` | `observables` | `.../observables` | array of observable objects (type, value, disposition, actions_taken[]) | No |
| `incidents` | `verdicts` | `.../verdicts` | array of verdict objects (type, observable, disposition, module, judgement_ids[]). **Note:** the vendor's own OpenAPI summary text for this operation says "Returns a list of events linked to this incident" -- that looks like a copy-paste artifact; the actual response schema is verdict-shaped, not event-shaped. Trust the schema, not that summary line. | No |
| `incidents` | `events` | `.../events` | array of event objects (type, observed_time, data.columns/rows, compacted_entities[]) | No |
| `incidents` | `mitre` | `.../mitre` | array of MITRE tactic objects, each with nested `techniques[]` -> `subtechniques[]` | No |
| `incidents` | `targets` | `.../targets` | array of asset/target objects (type, value, asset_id, asset_value, actions_taken[], properties[]) | No |
| `incidents` | `status` | `.../status` | object: completed, pending, sightings, indicators, verdicts counts | No |
| `incidents` | `indicators` | `.../indicators` | array of indicator objects (id, title, value, source, tags[]) | No |
| `incidents` | `primary-investigation` | `.../primary-investigation` | object: id, title/description fields -- **use this to get an investigation id** | No |
| `incidents` | `graph` | `.../graph` | graph representation (nodes/edges) | No |
| `incidents` | `errors` | `.../errors` | array of error objects reported by the incident's attached investigations (module, code, message) | No |
| `incidents` | `export` | `.../export` | large object: `incident_id`, `short_id`, plus `incident_summary.data` nesting the same summary/observables/indicators/targets shape as `summary` -- effectively "everything seen in XDR" for this incident in one call | No |
| `incidents` | `linked-casebooks` | `.../linked-casebooks` | array of full casebook objects (bundle, texts, observables) linked to this incident. Read-only cross-reference -- does not require or imply Casebook write access. | No |
| `incidents` | `linked-incidents` | `.../linked-incidents` | array of incident objects (same shape as a search row) linked to this incident | No |
| `incidents` | `report` | `.../report` | object with `timeline`/`executive`/`events`/`incident` keys, each `{content, ai_generated, created_at, updated_at, failed, generating, error}` -- the AI-generated narrative report, all sections in one call | No |
| `investigations` | `overview` | `GET /v2/investigation/{id}/overview` | object, same shape as incident overview minus `owner` | No -- not independently listable |
| `investigations` | `summary` | `.../summary` | object, same shape as incident summary | No |
| `investigations` | `entities`/`observables`/`verdicts`/`events`/`indicators`/`targets` | `.../<view>` | same per-view shapes as the incident equivalents above | No |
| `investigations` | `status` | `.../status` | object: investigation_id, completed, pending, sightings, indicators, verdicts | No |
| `investigations` | `graph` | `.../graph` | object: `{nodes, edges, errors[]}` | No |

`investigations` has no search/list entry and is therefore not part of the KG catalog walk (`refresh`/`ls`/`grep`/`sample` only cover `incidents`) -- there is no way to enumerate investigations independent of an incident or investigation id you already have.

## Uploading samples

Same provenance-anchored contract as other connectors in this repo: `upload-samples` requires either `--ref UUID` (from a prior `query-incidents` call's cache) or `--from-file FILE` (from that call's `--output`), plus the **exact same internal query description** the originating command used, and a `--catalog-path` under `/incidents/<field>`. Upload cap is 10 rows -- tighten `--limit` or filters before uploading rather than sampling in bulk. Investigation detail views are not sample-able (see Resources table).

## Edge-connectable fields

Useful scalar identity fields for cross-connector joins include:

- Incidents: `id`, `short_id`
- Observables/entities/targets (nested under incident/investigation detail views): `value` (paired with `type`, e.g. `type: "ip"`, `value: "10.0.0.5"`), `asset_id`

Do not edge against free-prose fields like `description`/`short_description` -- the inner-join requires whole-leaf equality.

## Rendering pattern

- **Incidents (search/get)** -- `id`, `short_id`, `title`, `status`, `severity`, `confidence`, `created`, `modified`, `scores.global`
- **Incident/investigation detail (`summary`, `overview`, `status`)** -- lead with the summary counts (`event_count`, `indicators.total_count`, `observables.total_count`, `targets.total_count`, or the `status` view's completed/pending counts) before drilling into nested `data[]` arrays
- **`entities`/`observables`/`verdicts`/`events`/`indicators`/`targets`** -- `type`, `value` (or `title`/`id` where there's no observable pair), `disposition` where present
- **`mitre`** -- tactic `title` + nested technique/subtechnique `external_id`/`title`

≤5 rows -> inline table. More than that -> show the first several plus a count of remaining matches, and offer to narrow the filters.

## Error handling

- **401/403** -- the connector's stored `client_id`/`client_secret` was rejected, the token host doesn't match the tenant's region, or the API Client's granted scopes don't cover the endpoint being called (see Auth recap's scope caveat). Surface this as a configuration issue -- do not ask the user to paste credentials into chat.
- **Region/host mismatch** -- a 401/403 that persists even after confirming the credential itself is valid may mean `api_host`/`token_host` don't match the tenant's actual region (US/EU/APJC); re-check the region against the XDR console login URL rather than re-trying the same call.
- **404 on `get-incident`/`incident-detail`/`investigation-detail`** -- the id doesn't exist, belongs to a different tenant/region, or (for `investigation-detail`) the investigation was never actually created/linked to an incident. Not the same failure as a permissions error -- don't retry with the same id.
- **429** -- Conure-side rate limiting; the vendor docs reviewed for this build did not state a specific requests/second or requests/minute figure for this API family (unlike some other Cisco XDR APIs), so back off and retry rather than assuming a specific window.
- **Zero-row `query-incidents` results** -- a real, valid empty result (printed as `No incidents results (0 rows).`), not a failure. Report plainly rather than retrying identically.
- **400** -- most likely a malformed filter value or an unsupported `--sort-by` field. Re-check the documented enums in Query/filter syntax above.
- **5xx** -- upstream Conure service issue; report the status and retry only if the user asks.

## Guardrails

- No write, mutation, or response actions exist on this connector -- it is read-only by design.
- Do not imply incident status changes, assignment changes, casebook creation, or investigation creation/mutation are available through this connector -- they are explicitly out of scope (see Scope).
- Do not expose or reconstruct `client_id`/`client_secret`, or the minted Bearer token.
- Do not treat the `assignees`/`categories`/`confidence`/`detection_sources`/`discovery_method`/`intended_effect`/`promotion_method` filters as confirmed multi-value/OR-capable -- the vendor spec does not document this.

## Connector base URL

Configure the connector's base URL as `https://<api_host>`, the tenant's region-specific Conure API host (`conure.us.security.cisco.com` / `.eu.` / `.apjc.`). The CLI sends full endpoint paths such as `/v2/incident/search` per call. OAuth2 token minting uses a separately-configured `token_host` (see Auth recap for why these two hosts are not the same field).

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- the skill auto-resolves its bound connector; the server mints and applies the OAuth2 Bearer token from the stored connector credential.

## Output format

`query-incidents` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). `get-incident`, `incident-detail`, `investigation-detail`, `view-result`, and KG commands return JSON.
