---
name: qradar
description: "MANDATORY for ALL IBM QRadar SIEM queries via the `qradar.py` CLI. READ THIS SKILL IN FULL before running the CLI. QRadar rejects common LLM defaults — timestamps are epoch milliseconds (not seconds, not ISO), filter AND/OR/NOT must be uppercase, string values must be single-quoted, a `Version` header of 14.0 is required on every call, and AQL is always async (submit → poll → fetch). The recipes below are the correct call shapes; don't trial-and-error the API. Covers offenses, Ariel/AQL events & flows, assets, log sources, and gated offense writes (note + close). IBM QRadar SIEM (on-prem / SIEM) only — for QRadar SOAR confirm the product variant first."
version: "0.1.3"
status: field-tested
paired_connector_type: qradar
---

# IBM QRadar SIEM

![status](https://img.shields.io/badge/status-field--tested-yellow) ![version](https://img.shields.io/badge/version-0.1.3-blue)

> **v0.1.3** — 2026-07-21 — Empty result sets now print an explicit `No <command> results (0 rows)` line instead of a blank line, so an agent can tell "query succeeded, nothing matched" from a silent failure (a blank output previously caused repeated retries). Output-only; no call-shape, query, or credential changes.
> **v0.1.2** — 2026-07-20 — Added a **Skip TLS verification** credential option (boolean, off by default) so operators can connect to on-prem consoles that present a self-signed / hostname-mismatched cert — previously there was no way to set this at connector creation. Trimmed the verbose `host` field description to one line (the full scheme/`/api` rationale stays in this skill). No call-shape changes.
> **v0.1.1** — 2026-07-17 — Field-tested against a live IBM QRadar tenant: offense search/get, AQL event & flow searches (submit → poll → fetch), asset-model lookups, log-source listing, and a gated offense-note write all confirmed against the real API. Clarified the `host` credential field description (bare host only — Crogl adds the scheme and `/api` path) after confirming a stray scheme/path is the top first-run failure. No call-shape changes.
> **v0.1.0** — 2026-07-16 — Initial community connector bundle. Migrated from the standalone `qradar-reference` cookbook into an importable connector type with a Python CLI (`scripts/qradar.py`), `connector.json` binding, and full KG support. Retrieval surfaces (offenses, AQL events/flows, assets, log sources) plus gated offense writes (note, close). Field values and build quirks carried over from the live-validated reference.

**STOP — read this entire skill before running the CLI.** QRadar rejects common LLM defaults: epoch-**milliseconds** timestamps (epoch seconds silently returns nothing), **uppercase** `AND`/`OR`/`NOT` in filters, **single-quoted** string values, and a mandatory `Version: 14.0` header on every call. AQL is always a three-step async flow. Trial-and-error wastes turns — the recipes below are validated call shapes.

One CLI: `scripts/qradar.py`. Run `scripts/qradar.py --help` for the subcommand list. This is a **data-retrieval cookbook**: compose the call, render the result, stop. No phase gates, no automated verdicts. Writes (offense note, offense close) are gated behind explicit user approval — see Guardrails.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Do not load a separate reference skill first — this bundle contains the operational call shapes needed to use the CLI.

## Fast Path

For a simple "show me open offenses" request, run the CLI directly:

```bash
python3 <cli_path> search-offenses --filter "status = 'OPEN'" --limit 5
```

## Subcommands

| Subcommand | Purpose |
|---|---|
| `search-offenses` | List/search offenses (`GET /api/siem/offenses`) with `--filter`, `--sort`, `--fields`. Returns grouped PSV + a result `ref`. |
| `get-offense` | Hydrate one offense by numeric ID (full detail card). |
| `aql` (alias `query`) | Run an Ariel AQL query over events/flows — QRadar's ad-hoc query language. Async submit → poll → fetch, handled internally. Returns grouped PSV + `ref`. |
| `search-assets` | Search the asset model by IP or hostname. |
| `list-log-sources` | List configured log sources. |
| `list-offense-types` | Resolve numeric `offense_type` IDs to names. |
| `list-closing-reasons` | List valid closing-reason IDs and text (needed to close an offense). |
| `add-offense-note` | **WRITE** — add a note to an offense. Gated behind `--yes`. |
| `close-offense` | **WRITE** — close an offense with a closing reason. Gated behind `--yes`; refuses to re-close a `CLOSED` offense. |
| `view-result` | Re-print one cached row from a prior search. |
| `refresh`, `ls`, `grep`, `upload-samples` | Standard KG catalog/sample commands. |

## Auth Recap

The connector stores two fields: `host` (the QRadar console hostname/IP, bound into `base_url` as `https://<host>`) and `api_token` (secret). crogld injects the token as the **`SEC` header** server-side. The agent must never construct the `SEC` header, prompt the user for the token, or add an `Authorization: Bearer` prefix — QRadar token auth is not Bearer.

Some on-prem installs use HTTP Basic auth instead; if so, the connector injects that. Either way the agent supplies no auth.

The CLI adds `Accept: application/json` and **`Version: 14.0`** to every call (not stored in the connector config). GET calls deliberately omit `Content-Type` — some builds reject it.

**TLS:** on-prem QRadar commonly uses a self-signed cert. Enable the **Skip TLS verification** option when creating the connector (off by default) — it's a per-connector setting, not per-call. A TLS handshake / certificate-verify error (e.g. `x509: certificate is valid for <internal-ip>, not <host>`) means it needs enabling.

## Context Budget

1. **Default limit** is 5 rows for `search-offenses`/`search-assets`; the CLI sends `Range: items=0-4`.
2. **Hard cap is 25 rows** (`Range: items=0-24`) — the CLI enforces this even if the user says "all"/"everything".
3. Always pass `--fields` to strip nested arrays / large prose you don't need; reduces payload and avoids the 30 KB auto-spill.
4. On AQL, never fetch without a bound `--limit` — unbounded Ariel result fetches on enterprise tenants can return hundreds of thousands of events.
5. After rendering the table the user asked for, drop the raw JSON from your reasoning; don't re-run identical queries within a turn.

## Query / Filter Syntax

QRadar offense/asset filters are a **SQL-like string** passed as `--filter`.

- Operators: `=`, `!=`, `<`, `>`, `<=`, `>=`, `AND`, `OR`, `NOT`, `IN`, `LIKE`, `BETWEEN`.
- **`AND`/`OR`/`NOT` must be uppercase** — lowercase silently fails or 422s.
- **String values single-quoted:** `status = 'OPEN'`, not `status = OPEN`.
- **Sort:** `-field` (descending), `+field` (ascending).
- **All timestamps are epoch milliseconds.** Epoch seconds (off-by-1000) silently returns nothing or 422s.
- **`LIKE`/`BETWEEN` do not work** on the nested asset field `interfaces(ip_addresses(value))` — use `=`/`!=` only.
- Resolve multiple IDs in one call with `IN`: `id IN (4, 17)` — never one call per ID.

Filter examples: `status = 'OPEN' AND magnitude >= 7` · `start_time > 1717772800000` · `offense_source = '<ip>'` · `id IN (12345, 12346)`.

### Field values (use only these)

| Field | Valid values |
|---|---|
| `status` (offense) | `OPEN` \| `HIDDEN` \| `CLOSED` (string, uppercase, single-quoted) |
| `severity` / `credibility` / `relevance` / `magnitude` | 0–10 **integer** (NOT a string label; filter with `>=`). `magnitude` is the best single triage proxy. |
| `follow_up` / `protected` / `inactive` | `true` \| `false` (boolean, unquoted) |
| `start_time` / `last_updated_time` / `close_time` | epoch **milliseconds** (integer) |
| `offense_type` | numeric ID — resolve to a name via `list-offense-types` before displaying |
| AQL search status | `WAIT` \| `EXECUTE` \| `SORTING` \| `COMPLETED` \| `CANCELED` \| `ERROR` |

AQL event/flow field names are **lowercase** (`sourceip`, `destinationip`, `username`, `logsourceid`) — a common LLM error is camelCase (`sourceIP`).

## AQL (events & flows)

AQL is QRadar's SQL-like query language. The CLI's `aql` command handles the mandatory three-step async flow (submit → poll → fetch) for you — you only supply the query expression.

- **No `LIMIT`** in the query string — returns `Field "LIMIT" does not exist`. Bound results with `--limit` (the CLI sends a `Range` header on the results fetch).
- **No `DISTINCT`** — use `GROUP BY` to deduplicate.
- **`ORDER BY` is incompatible with `LAST` time syntax.** When you need `ORDER BY`, replace `LAST N HOURS` with `START <epoch_ms> STOP <epoch_ms>` (unquoted integers).
- Always include a time range (`LAST N HOURS` or `START`/`STOP`) — omitting it defaults to the last 60 seconds.
- Do not `SELECT *` on events — the `payload` field alone can blow the 30 KB spill threshold.

## Common Recipes

| User asks | Command |
|---|---|
| "Show open offenses" | `search-offenses --filter "status = 'OPEN'" --limit 5` |
| "High-severity offenses" | `search-offenses --filter "status = 'OPEN' AND magnitude >= 7" --sort -magnitude --limit 5` |
| "Offenses in the last 24 hours" | `search-offenses --filter "start_time > <now_ms - 86400000>" --limit 5` |
| "Offenses for host `<ip>`" | `search-offenses --filter "offense_source = '<ip>'" --limit 5` |
| "Tell me about offense 12345" | `get-offense --id 12345` |
| "Search events for source IP `<ip>`" | `aql --query "SELECT starttime, sourceip, destinationip, username, eventcount FROM events WHERE sourceip = '<ip>' LAST 24 HOURS" --limit 25` |
| "Events correlated to offense 56" | `aql --query "SELECT starttime, sourceip, destinationip, eventcount FROM events WHERE INOFFENSE(56) LAST 7 DAYS" --limit 25` |
| "Find asset by IP `<ip>`" | `search-assets --filter "interfaces(ip_addresses(value = '<ip>'))" --limit 5` |
| "Find asset by hostname web01" | `search-assets --filter "hostnames(name = 'web01')" --limit 5` |
| "List log sources" | `list-log-sources --limit 25` |

### Accepted user date phrasings

Convert to epoch **milliseconds** before building the filter (reference instant `now_ms`):

| User phrasing | Conversion |
|---|---|
| "last 24 hours" / "last day" | `now_ms - 86400000` |
| "yesterday" | start `(now_ms - now_ms % 86400000) - 86400000`, stop `(now_ms - now_ms % 86400000) - 1` |
| "since 9am today" | `(now_ms - now_ms % 86400000) + 32400000` |
| "DD.MM.YYYY – DD.MM.YYYY" | parse start→start-of-day ms, end→end-of-day ms (+86399999) |

Ambiguous formats like `6/5/2026` (US vs EU) must be **clarified with the user** — never guess MM/DD vs DD/MM.

## Rendering Pattern

`search-offenses`, `aql`, `search-assets`, and `list-log-sources` emit a compact grouped PSV with a `ref`. Default columns:

- **Offenses:** id · description · severity · magnitude · status · start_time (human-readable).
- **Events:** starttime · sourceip · destinationip · username · eventcount.
- **Assets:** id · hostname · IP addresses · OS (from properties).
- **Log sources:** id · name · type_name · status.

Truncation: ≤5 rows inline; >5 show first 5 + "(N more, ask to see all)"; >50 summarize stats first. Render once, then drop the raw response from reasoning.

### Offense drill card ("tell me about offense N")

Surface: id · description (full) · severity/credibility/relevance/magnitude (side by side) · status · assigned_to · offense_source · **offense_type resolved to name** (via `list-offense-types` — never show the numeric ID alone) · start_time & last_updated_time (epoch ms → human-readable) · event_count/device_count/category_count · categories (first 5) · source/destination networks. Do **not** auto-pivot to events or assets unless the user asks.

## Error Handling

- **400 / 422** — usually a filter mistake: string not single-quoted, numeric field compared as a string, epoch seconds instead of ms, lowercase `and`/`or`, or `LIKE` on a nested asset IP field. Read the `message` in the body, fix, retry once. On AQL POST, check for a missing `FROM`, missing time range, or an unsupported keyword (`LIMIT`, `DISTINCT`).
- **401** — `SEC` token invalid or expired. Surface and **stop** — never retry. Advise re-issuing the token under QRadar Admin → Authorized Services.
- **403** — token lacks capability for this endpoint. Surface and stop.
- **404** on `/api/siem/offenses` — connector `host` is wrong or the base_url is malformed. Surface and stop.
- **AQL status `ERROR`** — the CLI reports `error_messages` and stops. Do not retry without user input.
- **AQL times out** (never reaches `COMPLETED`) — narrow the time range and retry.
- **503** — Ariel engine temporarily unavailable; wait and retry once.
- **TLS handshake / certificate verify failed** — the connector instance needs `skip_tls_verification: true` (self-signed cert). Advise enabling it in the connector config.

## Guardrails

- **No writes without explicit user approval** naming the target offense ID and action (e.g. "close offense 12345 with reason ID 3"). `add-offense-note` and `close-offense` refuse to run without `--yes`, which you pass only after the user approves.
- **Never re-close a `CLOSED` offense** — `close-offense` pre-checks status and refuses.
- **No automated verdicts** (CONFIRMED / LIKELY / FALSE-POSITIVE) — this is a retrieval cookbook, not a triage workflow.
- **No cross-domain sweeps** — if offenses carry `domain_id` (multi-domain deployment), ask which domain to scope to rather than sweeping all.
- **Render before pivoting** — answer the question asked; don't pre-emptively chain to events or assets.
- **Respect the user's scope** — "N hours" means exactly `now_ms - N*3600000`, not "yesterday" or "latest 25 by time".
- **Always resolve `offense_type` to a name** before displaying it.
- Never reveal the API token value.

## Required Runtime Env

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets are present in the agent container.
