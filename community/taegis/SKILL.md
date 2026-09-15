---
name: taegis
description: "Drive a Secureworks Taegis XDR (Sophos) connector via the taegis.py CLI. Search alerts and investigations with CQL, list endpoint assets with a structured filter, drill into a result, hydrate an entity by id, walk the catalog into the knowledge graph, and emit samples for edge learning. Talks to Taegis's single GraphQL gateway through a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container."
status: draft
version: "0.1.2"
---

# Secureworks Taegis XDR

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.2-blue)

One CLI: `scripts/taegis.py`. Run `scripts/taegis.py --help` for the subcommand list; `scripts/taegis.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Three resources are implemented — `alerts`, `investigations`, and `assets` (endpoint assets) — via Taegis's single GraphQL gateway. This connector is **read-only** (GraphQL queries only); there are no write/remediation actions (isolate endpoint, create/close investigation, add evidence) wired into it, even though the Taegis API exposes mutations for some of these.

## Changelog

- **0.1.2** — `--limit` on `query` is now code-enforced as `click.IntRange(1, 100)` (CONTRIBUTING.md "Every list/query command has a code-enforced result-size ceiling") -- previously a bare `type=int` with no ceiling. `_search`'s pagination loop now clamps `limit` to the same 100-row hard cap before it starts and caps each page request (offset/perPage/first) at 100 rather than asking for the full remaining budget in one page. A `query` that hits the hard cap while more rows exist upstream now prints a `Note: more ... are available upstream` line to stderr, surfacing the `total_results`/`totalCount`/`pageInfo` metadata every search document already requests but which the pagination loop itself didn't previously read. Still **not field-tested** — no live tenant.
- **0.1.1** — Docs/output only, no call-shape or credential change. Empty-result wording standardized to this repo's canonical phrasing (`qradar`/`jira-datacenter`): `No <resource> results (0 rows)` instead of the earlier `No <resource> found (0 results)`. Added a **Reference documentation** section to the provisioning guide linking the Taegis API docs, token/auth reference, CQL reference, and the `taegis-sdk-python` schema the GraphQL shapes were validated against. Still **not field-tested** — no live tenant.
- **0.1.0** — Initial draft. Alerts + investigations (CQL search) and endpoint assets (structured filter) over the GraphQL gateway, with the standard query / view-result / get / refresh / ls / grep / upload-samples surface. GraphQL operations/inputs/selection sets cross-checked against the `taegis-sdk-python` generated schema. **Not yet field-tested against a live tenant** — see "Draft caveats" below.

## Draft caveats (read before relying on output)

The GraphQL layer (operation names `alertsServiceSearch`/`alertsServiceRetrieveAlertsById`, `investigationsV2`/`investigationV2`, `assetsV2`; their input types, variables, and every queried field) was validated against the code-generated `taegis-sdk-python` schema — so it is schema-accurate, not merely doc-derived. What remains before this can be marked `field-tested`:

1. **OAuth2 token style (the real unknown).** Taegis's token endpoint (`/auth/api/v2/auth/token`) is documented as HTTP Basic auth (`client_id:client_secret`) with a JSON body `{"grant_type":"client_credentials"}`. `connector.json` uses `client_auth_method: "json"` (crogld sends the credentials in a JSON body — the closest match, and what the built-in Cribl connector uses). If the token mint fails, try `client_auth_method: "header"` (Basic-auth header, form-encoded body) instead. **crogld does *not* validate credentials or auth style at connector-create** — a `create-connector` with the wrong `client_auth_method` (or a bad secret) stores successfully with no probe, and the failure surfaces only on the **first query**, as a `401` when the token mint is attempted. So this choice must be confirmed by actually running a query through the connector; a clean `create-connector` proves nothing.
2. **Live-tenant confirmation.** The schema is verified but no query has actually run against a tenant. First live run: confirm rows return and the `created_at`/timestamp shapes (alerts return `{seconds, nanos}` objects; investigations/assets return ISO strings — an intentional schema asymmetry, already handled in the selection sets) render as expected. Any residual field correction is a one-line edit in the `RESOURCES` block of `scripts/taegis.py`.

## Auth recap

The connector stores three fields — `region_host`, `client_id`, `client_secret` — and crogld exchanges the client credentials server-side for a bearer token via `https://<region_host>/auth/api/v2/auth/token` (OAuth2 client-credentials grant), then attaches `Authorization: Bearer` automatically on every GraphQL call. Tokens are valid ~10 hours and re-minted automatically. Never ask the user for the client ID or secret during analysis, never print them, and never construct auth headers manually.

## Regional base URL

The connector's base URL is the Taegis **regional API host** — pick the one for the customer's tenant region. The same host is used for both the API and the token endpoint (`region_host` in the connect form, no scheme):

| Region | `region_host` |
|---|---|
| US1 | `api.ctpx.secureworks.com` |
| US2 | `api.delta.taegis.secureworks.com` |
| US3 | `api.foxtrot.taegis.secureworks.com` |
| EU1 | `api.echo.taegis.secureworks.com` |
| EU2 | `api.golf.taegis.secureworks.com` |
| AP (Sydney) | `api.hotel.taegis.secureworks.com` |

Additional regions exist (`india`, `juliet`, `kilo`, `quebec`); use the host Secureworks shows for the tenant. All are public, CA-signed HTTPS — no TLS-skip toggle is needed.

## Tool call shape

```bash
taegis.py query --resource alerts --filter "FROM alert WHERE severity >= 0.6 AND status = 'OPEN' EARLIEST=-1d" --limit 25
taegis.py query --resource investigations --filter "status = 'OPEN'" --limit 25
taegis.py query --resource assets --filter '{"assetState": ["Active"]}' --limit 25
taegis.py get --resource assets --id <asset-id>
taegis.py view-result --ref <ref> --row 1
taegis.py grep --pattern hostname
taegis.py ls --path /
```

`--limit` is **required** on `query` — there is no default, and the CLI errors immediately if it's omitted. It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped. `view-result` requires `--ref` and `--row` (1-based within the cached result); `--result-set` defaults to `0`. `--resource` on `query` and `get` defaults to `alerts` — always pass `--resource assets` or `--resource investigations` explicitly, or the query silently hits alerts.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `taegis.py query` (`--resource`, `--filter`, **`--limit` required, hard cap 100**, `--output`) | Search a resource through the GraphQL gateway. Paginates up to `--limit` (offset for alerts/investigations, cursor for assets); returns grouped PSV with a `ref` UUID for drill-down. Prints a stderr note if more rows exist upstream than `--limit` returned. `--output FILE` also writes rows in the verifiable upload-samples format. |
| `taegis.py get` (`--resource` defaults to `alerts`, `--id` required) | Hydrate one entity by id; no prior query ref needed. |
| `taegis.py view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full entity JSON). Pure cache read — Taegis returns full entities (the query's selection set) at search time. |
| `taegis.py grep` | String-search this connector's knowledge-graph catalog — matches `--pattern` against schema-element names, summaries, and whole tags. Literal substring by default; `--regex` / `--fuzzy` available. Reads persisted KG nodes, not the live API. |
| `taegis.py upload-samples` | Upload sample entities to the knowledge graph. Takes `--ref UUID` or `--from-file FILE`, plus the exact `--query` (the `--filter` value used to fetch them) and the `--catalog-path`. Refuses when results exceed a small row cap or the read-before-write check fails. |
| `taegis.py refresh` | Walk each configured Taegis connector's catalog (resource → field) into the KG. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks. Prunes stale entries. The only producer path — `ls` reads from KG nodes exclusively. Run after configuring a new connector; the weekly `kg-schema-refresh` job invokes it automatically. |
| `taegis.py ls --path /<abs-path> --format json` | List this skill's Taegis connectors' schema by UNIX path (Resource → Field). Field names discovered empirically from a one-row sample. JSON output; for human-friendly output call `kg_learner.py ls`. |

## Query model

`--filter` semantics differ by resource:

### alerts and investigations — CQL

Taegis's Common Query Language (CQL) statement. For alerts, `--filter` is the full statement:

```
FROM alert WHERE severity >= 0.6 AND status = 'OPEN' EARLIEST=-1d
```

- `severity` is a **decimal 0.0–1.0** (higher = more severe), *not* a Low/Medium/High enum: `>= 0.6` ≈ High-and-above, `>= 0.8` ≈ Critical. This is the single most common mistake — don't filter `severity = 'High'`.
- `status` values include `OPEN` and `RESOLVED`.
- Time window is set with `EARLIEST` / `LATEST` (relative like `-1d`, `-4h`, `-30m`). Bound the window — an unbounded alert scan is large.
- Empty `--filter` for alerts defaults to `FROM alert WHERE severity >= 0.6 EARLIEST=-7d`.

For investigations, `--filter` is the CQL WHERE-fragment (e.g. `status = 'OPEN'`); investigation `status` values include `OPEN`, `AWAITING_ACTION`, and closed states (`CLOSED_*`), and `priority` is numeric. Empty `--filter` returns recent investigations.

> The exact investigations CQL fields/values are doc-derived — verify against the live schema (see Draft caveats).

### assets — structured filter

Endpoint assets use a JSON `AssetFilter` object, **not** CQL. Pass it as the `--filter` string:

```bash
taegis.py query --resource assets --filter '{"assetState": ["Active"]}' --limit 25
```

Documented filter shapes: `assetState` (values include `Active`, `Healthy`) and a `where` clause with `or` for id lookups (`{"where": {"or": [{"id": "<id>"}]}}`). Empty `--filter` returns recent assets ordered by last update. Assets paginate by cursor; the CLI follows `pageInfo.endCursor` up to `--limit` automatically.

## Context budget rules

- `--limit` is mandatory; default to a small value (25 is reasonable for an analyst-facing question). Raise only on explicit request, and prefer tightening the CQL/filter first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, so there's no "just set --limit very high" option for bulk population; run multiple narrower `query`s instead.
- An empty filter still returns a broader unfiltered slice for knowledge-graph population, but it's bounded by the same 100-row hard cap as everything else — for an analyst question, add a CQL `WHERE` (or asset filter) when the user names a severity, status, or host.
- A `query` note on stderr ("more ... are available upstream") means the hard cap truncated the result, not that the filter matched nothing further -- narrow the CQL/filter rather than assuming the answer is complete.
- Render the main fields per the Rendering pattern; don't paste full raw JSON for more than a couple of entities inline. Drop raw PSV/JSON from reasoning after rendering. Don't re-run an identical `query` already in context — use `view-result` on the existing `ref`.

## Resources

- `alerts` — Taegis detections. `alertsServiceSearch` (CQL) + `alertsServiceRetrieveAlertsById` (get). Full entities at search time; paginated by offset.
- `investigations` — Investigations v2 cases. `searchInvestigationsV2` + `investigationV2`. Fields: `shortId`, `title`, `priority`, `status`, `assigneeId`, `keyFindings`, timestamps. Offset pagination.
- `assets` — endpoint asset inventory (`assetsV2`). Fields: `hostId`, `hostnames`, `ipAddresses`, `osFamily`/`osVersion`, `endpointType`, `sensorVersion`, `isolationStatus`, `lastSeenAt`. Cursor pagination.

Every request's per-page size (offset's `limit`, investigations' `perPage`, assets' `first`) is capped at the same 100-row hard cap rather than asking for the full remaining `--limit` budget in one page.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded entity must be the byte-identical output of a recent `query` from this same container. Two ways to satisfy it:

1. **`--ref UUID`** — the `Result reference: <uuid>` line `query` prints; the container cache holds the returned entities.
2. **`--from-file FILE`** — pass a file written by `query --output FILE`; its JSON frontmatter carries a sha256 over the rows, so a hand-edited file is rejected.

Either way, the **exact `--filter` value (passed as `--query`)** used for the originating `query` must be passed to `upload-samples`; a mismatch fails the read-before-write check. Pass `--query ""` when the query used no filter. The upload cap is small (a handful of rows) — tighter filters and `--limit 1` are the expected pattern. `--catalog-path` (required) is `/<resource>/<field>` (e.g. `/assets/hostnames`); tiers are created lazily server-side and nested JSON keys are walked into child nodes.

## Edge-connectable fields

When picking source/target nodes for `kg_learner.py create-edge`, use **scalar identity fields** — the server's inner-join intersects bare JSON string leaves between two `data_samples`:

- Endpoint asset (from `assets`): `hostId`, `id`, `hostnames`, `ipAddresses` — the device join keys.
- Alert → device / investigation: `investigation_ids` links an alert to an investigation; asset identity fields (hostname, IP) are what tie an alert or investigation back to a device, and line up with a CrowdStrike host, Defender machine, or Sentinel entity on other connectors.

Endpoint assets are the connective tissue here: learning their identity fields into the KG is what lets an alert or investigation resolve to a real device across connectors. Do **not** edge against free prose like `keyFindings` or an alert `description` — the inner-join requires whole-leaf equality.

## Rendering pattern

Render a compact table — don't dump raw PSV/JSON as the answer:

- **Alerts** — severity (0.0–1.0) · status · title · created_at (epoch `seconds`) · investigation_ids.
- **Investigations** — shortId · priority · status · title · assigneeId · createdAt.
- **Assets** — hostnames · ipAddresses · osVersion · endpointType · isolationStatus · lastSeenAt.

≤5 rows → inline table. More than that → show the first several plus a count of remaining matches, and offer to narrow the filter.

## Error handling

- **GraphQL `errors` (HTTP 200 with an `errors` array)** — a bad query: unknown field, malformed CQL, or a selection set that doesn't match the live schema. The CLI surfaces the upstream message. Don't retry the same query — fix the field/CQL (or reconcile the selection set per Draft caveats). GraphQL reports these in-band, not via status code.
- **400** — malformed request body. Check the CQL statement / JSON asset filter.
- **401** — bearer token invalid/expired; self-heals via the connector's OAuth flow. If it persists, the connector's stored client credentials may be invalid, revoked, or the wrong `client_auth_method` — surface it as a connector-config issue, never ask the user to paste a token into chat.
- **403** — the API client lacks the role/scope for that resource. Surface as a configuration issue for whoever created the client credentials, not something the agent can work around.
- **429** — Taegis rate-limits across all APIs; back off and report the limit rather than retrying in a loop.
- **5xx** — upstream service issue; report the status and retry only if the user asks.

## Guardrails

- Read-only. No write/remediation actions exist on this connector — do not imply endpoint isolation, investigation creation/closure, or evidence changes are possible through it, even though the Taegis API itself supports those mutations.
- Don't treat a single alert's `status`/`resolution_reason` as a final verdict without corroborating evidence.
- Do not expose or reconstruct the client ID or client secret.
- Don't fabricate data when a query returns zero results — report zero plainly and suggest loosening the CQL/filter or widening the `EARLIEST` window.
- Severity is a 0.0–1.0 decimal, not an enum — see Query model.

## Connector base URL

Configure the connector's base URL as the Taegis regional API root (`https://<region_host>` — see the region table). The CLI POSTs GraphQL documents to `/graphql`.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets — the skill auto-resolves its bound connector; the server applies the OAuth Bearer stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
