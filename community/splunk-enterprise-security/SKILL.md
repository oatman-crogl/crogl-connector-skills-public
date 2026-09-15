---
name: splunk-enterprise-security
description: "Drive Splunk Enterprise Security's Mission Control API (investigations, findings, notes, risk scores, assets, identities) via the `splunk_enterprise_security.py` CLI. Read-only -- no write actions exist on this connector. Covers investigations and findings search/get, notes, risk-entity scores, and id-only asset/identity lookups. Does NOT cover core Splunk SPL/event search -- that is a separate product surface. Built from Splunk's official ES 8.3 API reference and browser-rendered endpoint pages, not live-verified against a real tenant -- treat time-window defaults and capability-gating notes below as vendor-documented, not field-confirmed."
status: draft
version: "0.1.0"
---

# Splunk Enterprise Security

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** -- 2026-07-20 -- Initial community connector bundle, built docs-only from Splunk's official ES 8.3 API reference (`help.splunk.com/.../splunk-enterprise-security-8/api-reference/8.3/...`), verified page-by-page via a live browser render rather than trusting a search-result summary. Read-only: search/get investigations, search/get findings, get investigation notes, get risk scores for an entity, get one asset/identity by KV-store id. No credentials were available for this build -- nothing below has been exercised against a real stack. See the frontmatter for the scope boundary against a possible future core-Splunk SPL connector.

**STOP -- read this entire skill before running the CLI.** This connector is scoped to Splunk Enterprise Security's **Mission Control API** (`/servicesNS/nobody/missioncontrol/public/v2/...`) only. It does not run SPL, does not search raw events/indexes, and does not expose write actions (no create/update investigation, no notes write, no risk modifiers). If a user asks for a raw index search (`index=... | stats ...`), that is out of scope for this connector -- say so rather than improvising against these endpoints.

One CLI: `scripts/splunk_enterprise_security.py`. Run `scripts/splunk_enterprise_security.py --help` for the subcommand list; `scripts/splunk_enterprise_security.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Do not load a separate reference skill first -- this bundle contains the operational call shapes needed to use the CLI.

## Fast Path

For a simple "show me open investigations" request, run the CLI directly:

```bash
python3 <cli_path> search-investigations --status New --limit 5
```

## Subcommands

| Subcommand | Purpose |
|---|---|
| `search-investigations` | List/search investigations (`GET .../investigations`) filtered by `--status`, `--urgency`, `--sensitivity`, `--disposition`, `--owner`, `--ids`, or a create/update time range. Returns grouped PSV + a result `ref`, trimmed to a curated column set (see Context Budget). |
| `get-investigation` | Hydrate one investigation by GUID or `display_id` (e.g. `ES-00001`) -- the full untrimmed record. There is no dedicated get-by-id endpoint; this is `search-investigations` with `--ids` set to one value. |
| `get-investigation-notes` | List notes on an investigation or finding, with `--search` keyword filtering and `--type Task\|Incident\|All`. |
| `search-findings` | List/search findings (`GET .../findings`) filtered by `--status`, `--urgency`, `--disposition`, `--owner`, `--rule-title`, `--finding-ids`, or `--earliest`/`--latest`. Returns grouped PSV + a result `ref`. |
| `get-finding` | Hydrate one finding by its `event_id`. |
| `get-risk-scores` | Get risk scores for a risk entity (an IP, username, hash, etc.) -- `--entity` required. |
| `get-asset` | Fetch one asset by its `assets_by_str` KV-store id. **id-only** -- see Assets & Identity below. |
| `get-identity` | Fetch one identity by its KV-store id. **id-only** -- see Assets & Identity below. |
| `view-result` | Re-print one cached row from a prior search. |
| `refresh`, `ls`, `grep`, `upload-samples` | Standard KG catalog/sample commands, scoped to the `investigations` and `findings` surfaces only. |

## Auth Recap

The connector stores two fields -- `stack` (bare `host:port`, e.g. `splunk.example.com:8089`; Crogl adds `https://`) and `auth_token` (secret) -- bound to `https://{stack}/servicesNS/nobody/missioncontrol`. crogld injects `Authorization: Bearer <token>` server-side on every call. The agent must never construct this header, ask the user for the token, or print it.

Splunk Enterprise Security gates each endpoint behind its own capability, checked against the token's owning role:

| Surface | Capability required (read) |
|---|---|
| Investigations, findings, notes | `mc_investigation_read` |
| Risk scores | `mc_risk_score_write` -- **a documented vendor naming quirk**: reading risk scores requires a capability literally named `_write`. This is not a typo in this connector; a token scoped only to obviously-read-sounding capabilities will 403 on `get-risk-scores` until this specific capability is granted. |
| Assets | `mc_assets_read` |
| Identities | `mc_identity_read` |

`admin_all_objects` is a documented blanket alternative to any of the above. The management port is `8089` by default for both Splunk Enterprise and Splunk Cloud; only override it if an admin has changed it.

**TLS:** self-managed Splunk Enterprise installs commonly use an internal CA or self-signed cert. Enable **Skip TLS verification** at connector creation (off by default) if the stack presents one -- it's a per-connector setting, not per-call.

## Context Budget

1. **Default limit** is 5 rows for every list command; the CLI sends `limit` as a real query parameter to the vendor (not just client-side truncation).
2. **Hard cap is 25 rows** even if the user says "all"/"everything" -- the vendor's own ceiling is 100, but this CLI caps tighter to protect context.
3. `search-findings` accepts `--fields` and defaults to a curated subset (`event_id,rule_title,status_label,urgency,severity,disposition_label,owner,risk_object,risk_object_type,risk_score,notable_type,_time`) -- pass your own comma list to widen or narrow it.
4. **`search-investigations` has no server-side field filter** (unlike findings) -- every investigation is a heavy object with nested `response_plans`/`findings`/`consolidated_findings` sub-objects. This CLI trims the list view to a fixed scalar column set (`investigation_id`, `name`, `status_name`, `urgency`, `sensitivity`, `disposition_name`, `owner`, `count_findings`, `risk_score`, `create_time`, `update_time`); use `get-investigation` or `view-result` for the full record.
5. Render the answer the user asked for, then drop raw JSON/PSV from reasoning. Don't re-run an identical search already in context -- use `view-result` on the existing `ref` instead.

## Query / Filter Syntax

Splunk ES's Mission Control API exposes a **fixed set of named query parameters per endpoint**, not a free-form filter string or a query language (SPL/AQL/KQL). Pass values through the CLI's own options -- don't try to compose a filter expression.

### Field values (use only these)

| Field | Valid values |
|---|---|
| `urgency` (investigations, findings, risk-score entity search) | `informational` \| `low` \| `medium` \| `high` \| `critical` \| `unknown` |
| `sensitivity` (investigations) | `White` \| `Green` \| `Amber` \| `Red` \| `Unassigned` |
| `status` / `disposition` | Free-text id or label configured per-tenant under **Configure -> Findings and Investigations -> Status/Disposition** -- there is no fixed enum; don't guess a value the tenant hasn't configured. `notable_type` on findings is a real fixed enum: `notable` \| `risk_event`. |
| `entity_type` (risk scores) | Comma-separated list: `user`, `system`, `hash_values`, `host_artifacts`, `tools`, `other`. |
| `sort` | `<field>:asc,<field>:desc` for investigations/findings (e.g. `create_time:asc,status:desc`); notes only accept `create_time:1`, `create_time:-1`, `update_time:1`, `update_time:-1`; risk scores only sort on `risk_score` or `entity_type`. |
| Time params (`--create-time-min/max`, `--update-time-min/max`) | Epoch **seconds** on investigations (not ms). |
| Time params (`--earliest`/`--latest` on findings, notes search, risk scores) | Relative (`-30m`), epoch seconds, or ISO 8601 -- these accept a looser format than the investigation time filters above. Don't conflate the two families. |

### Time-window traps

- **`get-finding` defaults to a 24-hour lookback on `--earliest` per the vendor docs if you omit it** -- a real, older finding will silently 404 even though the `event_id` is correct. This CLI defaults `--earliest` to `-7d` instead of leaving it unset, specifically to avoid that trap; widen it further (`-30d`, an ISO timestamp, etc.) if the finding is still not found.
- `search-findings` (the list endpoint) states no default for `--earliest` in the vendor docs -- omitting it does not appear to time-bound the search, unlike `get-finding`. This asymmetry is vendor-documented but not live-confirmed; if a search you expect to match returns nothing, try passing an explicit wide `--earliest` before concluding the filter is wrong.
- `--latest` defaults to "now" if omitted, on both `search-findings` and `get-finding`.

## Resources

- **Investigations** (`search-investigations`, `get-investigation`, `get-investigation-notes`) -- case objects that findings roll up into. `incident_origin` on an investigation names where it came from (e.g. `ES Notable Event`, a risk-based-alerting finding).
- **Findings** (`search-findings`, `get-finding`) -- the individual notable/risk events (`notable_type`: `notable` or `risk_event`) that investigations aggregate. A finding's `event_id` has the shape `<guid>@@notable@@<hex>`.
- **Risk scores** (`get-risk-scores`) -- current risk score(s) for an entity (IP, username, hash, ...), broken out by `entity_type`. Omitting `--earliest`/`--latest` reads from cached risk-lookup tables rather than running a live search against the Risk data model, per the vendor docs -- results may lag if those lookup-generating saved searches aren't enabled on the tenant.
- **Assets & Identity** (`get-asset`, `get-identity`) -- **id-only lookups.** The API has no filter-by-IP, filter-by-hostname, filter-by-email, or filter-by-username, and no list/search endpoint at all for either resource -- only fetch-by-KV-store-id. Nothing returned by investigations/findings/risk-scores directly hands you that KV id (a finding's `risk_object` gives you an IP or username *value*, not the KV id needed to call `get-asset`/`get-identity`). In practice these two commands are only useful when an analyst already has the id from the Splunk ES UI or another export. Don't imply to the user that "look up the asset for this IP" is possible through this connector -- it isn't, without that id in hand already.

## Common Recipes

| User asks | Command |
|---|---|
| "Show new investigations" | `search-investigations --status New --limit 5` |
| "High-urgency investigations" | `search-investigations --urgency high --sort urgency:desc --limit 5` |
| "Tell me about investigation ES-00001" | `get-investigation --id ES-00001` |
| "Notes on that investigation" | `get-investigation-notes --id ES-00001` |
| "Open findings" | `search-findings --status New --limit 5` |
| "Findings for rule X" | `search-findings --rule-title "24 hour risk threshold exceeded" --limit 5` |
| "Tell me about finding `<event_id>`" | `get-finding --id "<event_id>"` |
| "Risk score for user bad_user@splunk.com" | `get-risk-scores --entity bad_user@splunk.com` |

## Rendering Pattern

`search-investigations`, `search-findings`, `get-investigation-notes`, and `get-risk-scores` emit a compact grouped PSV with a `ref`. Default columns:

- **Investigations:** investigation_id · name · status_name · urgency · sensitivity · disposition_name · owner · count_findings · risk_score.
- **Findings:** event_id · rule_title · status_label · urgency · severity · disposition_label · owner · risk_object · risk_score · _time (unless `--fields` was overridden).

Truncation: ≤5 rows inline; >5 show first 5 + "(N more, ask to see all)". Render once, then drop the raw response from reasoning.

## Error Handling

- **401** -- `auth_token` invalid or expired. Surface and **stop** -- never retry. Advise re-issuing the token under Splunk **Settings -> Tokens**.
- **403** -- the token's role lacks the capability for this specific endpoint (see the capability table in Auth Recap). Check the *exact* capability named in the error, not just "does the role have read access" -- `get-risk-scores` in particular needs a capability named `_write` despite being a GET. Surface as a configuration issue for whoever provisioned the connector.
- **400** -- usually a bad filter value: a `status`/`disposition` string that isn't configured on this tenant, a malformed time value, or a field name typo in `--fields`. Read the response body, fix, retry once.
- **404** on `get-investigation`/`get-finding`/`get-asset`/`get-identity` -- either the id is wrong, or (for `get-finding`) the default/given `--earliest` window is too narrow to include an older finding. Widen `--earliest` before concluding the id is invalid.
- **429** -- rate-limited; back off and report rather than retrying in a loop.
- **500** -- upstream Splunk Enterprise Security issue; report the status and retry only if the user asks.
- **Empty result on a 200** -- a real, correct empty result, not an error. Distinguish "no investigations/findings match this filter" from "wrong filter value" by checking whether the `status`/`disposition` string used is actually one this tenant has configured (see Query/Filter Syntax).

## Guardrails

- **No write actions exist on this connector.** Do not imply that creating/updating an investigation, writing a note, or adding a risk modifier is possible through it, even though those endpoints exist in the vendor API -- they are deliberately not wired into this CLI.
- **Do not attempt raw SPL / index search** through this connector -- it is out of scope (see the top of this skill).
- **Never fabricate investigation/finding data** when a search returns zero results -- report zero results plainly and suggest loosening the filter or widening the time window.
- **Don't claim asset/identity lookup-by-attribute works** -- `get-asset`/`get-identity` are id-only; see Assets & Identity above.
- Never reveal the `auth_token` value.

## Connector base URL

Configure the connector's `stack` field as `<host>:8089` (or the tenant's customized management port) -- Crogl binds this into `https://{stack}/servicesNS/nobody/missioncontrol`. Every CLI path is relative to that root (e.g. `/public/v2/investigations`).

## Required Runtime Env

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets are present in the agent container.

## Output Format

List commands (`search-investigations`, `search-findings`, `get-investigation-notes`, `get-risk-scores`) return grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). `get-investigation`, `get-finding`, `get-asset`, and `get-identity` return full JSON.
