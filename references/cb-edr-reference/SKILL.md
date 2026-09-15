---
name: cb-edr-reference
description: "MANDATORY for ALL Carbon Black EDR queries. READ THIS SKILL IN FULL before making any API call to the carbon_black_edr connector. CBR 7.9 has critical Solr quirks (no Z suffix on timestamps, no NOW literals, mandatory sort=, GET-only alert search — POST silently drops filters). Do NOT trial-and-error the API — every failed attempt wastes a turn. The recipes below are the ONLY correct call shapes. Covers: alerts, processes, sensors, binaries, containment. Carbon Black EDR ONLY — for Carbon Black Cloud use cbc-reference."
version: "1.3.1"
---

# Carbon Black EDR (on-prem) — Reference / Ad-hoc Query Cookbook

> **v1.3.1 (2026-06-23) — Skill priority enforcement.** Strengthened the description and header to force the agent to read this skill before making any CB EDR API calls. Testing showed the agent was trial-and-erroring the API for 13 turns before reading the skill, hitting every documented anti-pattern (POST alerts, /api/v2/process, Z-suffixed timestamps). The recipes were correct — the agent just wasn't reading them first.
>
> **v1.3.0 (2026-06-23) — Python CLI connector migration.** Updated connector selection and tool call shape for the post-2026-06-19 connector architecture (Go plugins → API proxy + Python CLI). Agent now discovers connectors via `list_connectors` MCP tool and issues HTTP calls through the connector's auto-generated `proxy.py http` CLI. All recipes, Solr quirks, error handling, and rendering rules are unchanged — only the transport layer changed.
>
> **v1.2.6 (2026-05-19) — Connector terminology pass.** Replaced "api-proxy" framing with "connector" throughout (matches the Crogl-wide rebrand to Custom Connectors). Connector-discovery instructions now match on vendor substring rather than the literal `api_proxy_` prefix so the skill survives the upcoming MCP-tool prefix change. No recipe or behavior changes.
>
> **v1.2.5 — Generalized for the shared connector-skills repo.** Removed customer-specific hostnames, domains, and "POC-bug" framing. Substantive CBR 7.9 behavior (Solr no-`Z` quirk, `NOW` literal rejection, missing v2/v3 endpoints, mandatory `sort=`, `process_name` vs `cmdline` distinction) is preserved verbatim — that's upstream build behavior, not customer-specific.
>
> **v1.2.4 — Two gaps surfaced by permutation testing on a CBR 7.9.1 test install.** (1) `process_name` indexes the **binary basename** (kernel exec name), not argv[0] / script name. `python3 /tmp/lazagne.py` indexes as `process_name=python3`; the script name is only findable via `cmdline:*lazagne*`. Added a "find scripts" recipe and a 0-result fallback rule. (2) `rows=` hard cap was ignored when the user said "no row limit" / "all" — agent issued `rows=1000` and the 30 KB auto-spill caught it but at the cost of 1.7 MB transferred. Tightened the rows-cap rule to be enforced even against explicit user requests for "all".
>
> **v1.2.3 — Rule scope fix.** Live test against a CBR 7.9 install reproduced the drop-the-time-filter anti-pattern: agent applied the "no `NOW`" rule only to the alert section, used `last_update:[NOW-6HOURS TO NOW]` on `/api/v1/process`, got 400, dropped the time filter entirely → unbounded 200. Promoted the rules to a "Universal time-range rules" block (this section) that applies to **every** endpoint, and tightened 400-recovery to use absolute timestamps *first* before falling back to client-side filtering.
>
> **v1.2.2 — Coverage additions.** Added top-line recipes for "alerts mentioning process `<X>`" (was buried in field table) and "describe rule `<R>`" (was uncovered). Explicit date-format note: accept DD.MM.YYYY and relative phrases, convert to ISO `2026-05-13T00:00:00` (no `Z`) before composing the call.
>
> **v1.2.1.** Pulled from trace analysis of an on-prem CBR 7.9.0.251016.0400 install:
>
> - **Solr time-range timestamps must omit the `Z` suffix.** `last_update:[2026-05-11T00:00:00Z TO 2026-05-12T23:59:59Z]` → 400. `last_update:[2026-05-11T00:00:00 TO 2026-05-12T23:59:59]` → 200. Same pattern applies to alerts.
> - **Endpoints that do NOT exist on this build (all 404, do not probe):** `/api/v3/alert/_search`, `/api/v2/process`, `/ping`, `/v1/health`. Use only the endpoints in the catalog below.
> - `GET /api/v2/alert?q=hostname:<X>` filters at the index correctly. **Without `sort=created_time desc`, server returns oldest-first** (so `rows=5` gives the 5 oldest, often multi-year-old, alerts). `sort=created_time desc` is mandatory.
> - `POST /api/v2/alert` with `criteria` body still does NOT filter on this build — never use POST for alert search.
> - `GET /api/info` confirms identity (`cloud_install:false`, version, default page sizes `processPageSize:10`, `binaryPageSize:10`). Don't call it on every invocation.

**STOP — read this entire skill before making any call to the CB EDR connector.** CBR 7.9 rejects common LLM defaults (NOW literals, Z-suffixed timestamps, POST alert search, /api/v2/process). Trial-and-error wastes turns. The recipes below are validated against a live CBR 7.9.1 install — use them exactly.

Cookbook style: no phase gates, no STOP gates, no write-back. Read the catalog, build the call, render the result, stop.

For end-to-end investigations (alert → fleet sweep → process tree → containment), use a workflow skill instead.

## Connector selection (run once at start)

1. Call the `list_connectors` MCP tool to get the list of configured connectors.
2. Find the connector whose `name` contains `carbon_black_edr`, `cb-edr`, `cb-response`, `cbedr`, or `carbon-black`. **Never pick `carbon_black_cloud` / `cbc`** — different product.
3. If ambiguous or none, list what exists and ask.
4. Note the connector's `cli_path` — this is the Python CLI script. All HTTP calls go through it via the `http` subcommand.

The connector's CLI is at the `cli_path` from `list_connectors`. Use the `http` subcommand for all requests:

```
python3 <cli_path> http --method GET --path /api/v2/alert --query q="hostname:X" --query rows=5 --query sort="created_time desc"
```

**CB-EDR is single-tenant per install. There is no `org_key`, no tenant identifier, no API ID, no Bearer token. Never ask the user for one.** Auth is a single `X-Auth-Token` header which the connector injects. If you find yourself prompting the user for an `org_key`, you're confusing CB-EDR with Carbon Black Cloud — stop and reread this section.

Do **not** call `GET /api/info` as a routine smoke test. The connector is pre-validated at install. Skip it.

## Universal time-range rules (READ FIRST — applies to ALL endpoints)

These four rules apply to every CBR Solr query — alerts, processes, binaries, anything that takes `q=`. Live-validated on CBR 7.9.x.

1. **Absolute timestamps only.** `NOW`, `NOW-1DAY`, `NOW-6HOURS` → **400 `query_syntax_error`** on this build. Always compute absolute ISO timestamps before composing the call.
2. **No `Z` suffix.** `[2026-05-15T00:00:00Z TO …]` → 400. `[2026-05-15T00:00:00 TO …]` → 200. Same Solr-version quirk for both alert and process indexes.
3. **Mandatory `sort=<time-field> desc`.** Without `sort=`, Solr returns oldest-first (insertion order), so `rows=5` gives you the 5 oldest rows out of however many match. Field per index:
   - Alerts: `sort=created_time desc`
   - Processes: `sort=last_update desc`
4. **Mandatory time predicate.** Always include `created_time:[…]` (alerts) or `last_update:[…]` (processes) — even for "the last 6 hours". Dropping the time predicate on an unbounded tenant returns 25 oldest of N-thousand rows. **Never** drop the time filter as a 400-recovery strategy.

### 400-recovery procedure (canonical)

When you get `400 query_syntax_error` on a time-bounded query:

| Step | What | Why |
|------|------|-----|
| 1 | If timestamps had `Z`, strip them and retry. | Most common cause. |
| 2 | If using `NOW`/`NOW-NHOUR`, convert to absolute ISO (computed against current UTC) and retry. | Second most common cause. |
| 3 | If field name is `start:` or `created_at:` on processes, switch to `last_update:`; if `start:` or `last_update:` on alerts, switch to `created_time:`. | Field-name drift. |
| 4 | If still 400 after steps 1–3, fall back to client-side filtering: drop the time predicate, fetch with `rows=5&sort=<time-field> desc`, then filter the response array on the time field. | Last resort, never first. |

**Do not skip steps.** Do not jump to step 4 because the model "remembers" CBR usually accepts `NOW` — on this build it doesn't, and steps 1–3 fix the actual problem.

### Endpoint version pin

CBR 7.9 supports **only the versions listed in the catalog below**. The most common LLM error is to default to `/api/v2/process` (matches the alert side's v2). **There is no v2 process endpoint.** Use `/api/v1/process`. Likewise no `/api/v3/alert/_search`.

If your *first* tool call is to `/api/v2/process` or any path outside the catalog, you have skipped this section — go back and read the catalog before issuing the call.

## Auth recap

- Single header: `X-Auth-Token: <token>` (injected by the connector)
- No org_key, no Bearer prefix, no HMAC
- `skip_tls_verification: true` is the on-prem default

## Tool call shape (critical)

Use the connector's CLI `http` subcommand. **Never embed `?…` in `--path`** — crogld URL-encodes the path value, so a `?` becomes `%3F` and you get a 404. Pass query parameters as separate `--query key=value` arguments.

```bash
python3 <cli_path> http --method GET --path /api/v2/alert --query q="hostname:X" --query rows=5 --query sort="created_time desc"
```

The response is a JSON object with `status_code` and `body` fields. Parse `body` as JSON for the CBR response payload.

## Context budget rules

1. **Default `rows=5`** for every call. Bump to 10 only if the user explicitly asks "show me more". **Hard cap `rows=25` — never exceed, even if the user says "all", "no row limit", "everything", or names a higher number.**
   - When the user explicitly asks for "all" / >25 results: issue `rows=25` anyway, then respond with the rendered 25 plus the `total_results` count: *"Showing the 25 most recent of N total matches. Want me to narrow the window or filter further?"* Never issue `rows=1000` or `rows=10000` even when CBR's `maxRowsSolrReportQuery` would permit it.
   - The cap is about agent context budget, not server capability. Bypassing it on high-volume hosts (where a single hostname can have 100K+ matching events) returns multi-MB responses and burns context.
2. **Always include `sort=<time-field> desc`** so `rows=5` returns the newest, not the oldest.
3. **Hard cap: if a tool result exceeds 30 KB, STOP** and re-issue with tighter scope (smaller rows, a `hostname:` predicate, etc.).
4. **Summarize and discard.** After rendering the table the user asked for, drop the raw JSON from your reasoning. If a follow-up needs the same data, work from the table you already produced.
5. **Don't re-run identical queries.** The prior result is already in context.

## Alert search — `GET /api/v2/alert?q=<solr>` only

Two rules govern every call:
1. **Always include `sort=created_time desc`.** Without it, the server returns oldest-first and you'll render years-old alerts.
2. **Time predicates in Solr `q=` must NOT use the `Z` suffix on timestamps.** With `Z`: 400. Without `Z`: 200.

### Query recipes

| User intent | Call |
|-------------|------|
| Alerts on host `<X>` (any time) | `q=hostname:<X>` |
| Alerts in last `<N>` hours (try server-side first) | `q=created_time:[<now-Nh, no Z> TO <now, no Z>]` |
| Alerts on host `<X>` in last `<N>` hours | `q=hostname:<X> AND created_time:[<from, no Z> TO <to, no Z>]` |
| Critical alerts in last `<N>` hours | `q=severity:critical AND created_time:[<from, no Z> TO <to, no Z>]` |
| Alerts mentioning process `<P>` (in last `<N>` hours) | `q=process_name:<P> AND created_time:[<from> TO <to>]` — add `AND hostname:<X>` if a host is named |
| Alerts matching rule `<R>` | Fetch by host-or-time (rows=5, sort=created_time desc) then **client-side** filter `r["reason"]` contains the rule substring. Don't put long rule strings in `q=` — hyphens, dots, parens trip Solr. |
| Describe rule `<R>` / what is rule `<R>` | Same as "Alerts matching rule" — fetch ≥1 alert whose `reason` matches, then render a rule-summary card (next section) instead of the alert table. |

Every call carries `rows=5&sort=created_time desc`. Bump `rows` to 25 max only on explicit user request.

### Rule-summary card (for "describe rule" asks)

When the user wants the rule explained rather than the alert list, render:

- **Rule name** — `reason` (cleaned: trim leading/trailing punctuation)
- **Alert type** — `alert_type` (e.g. `watchlist.hit.query.process`, `watchlist.hit.ingress.process`)
- **Watchlist** — `watchlist_name` (#`watchlist_id`)
- **Feed** — `feed_name` (#`feed_id`) if present (TI feed) or "Custom watchlist" if `feed_id == -1`
- **Underlying Solr query** — `ioc_attr.highlights[0].query` if present, else `ioc_attr` raw. This is the high-value field: it shows the analyst exactly what pattern fires the rule.
- **Severity / threat** — `severity`, `threat_id`, `report_score` if present
- **Recent hits** — `total_results` from the search response (how many times the rule has fired in scope)

If multiple alerts came back, pick the first (newest) for the descriptive fields — they should be identical across hits of the same rule.

**Example (last 24h on host `backup`, computed at 2026-05-14T13:00:00):**
```bash
python3 <cli_path> http --method GET --path /api/v2/alert \
  --query q="hostname:backup AND created_time:[2026-05-13T13:00:00 TO 2026-05-14T13:00:00]" \
  --query rows=5 --query sort="created_time desc"
```
Note: **no `Z`** on the timestamps. Compute window absolutely; `NOW`/`NOW-1DAY` are not accepted.

**Accepted user date phrasings — convert before composing the call:**

| User says | Convert to (no `Z`) |
|-----------|---------------------|
| "last 24 hours" / "last day" | `[<now-24h> TO <now>]` |
| "yesterday" | `[<yesterday 00:00:00> TO <yesterday 23:59:59>]` |
| "12.05.2026 - 13.05.2026" (DD.MM.YYYY) | `[2026-05-12T00:00:00 TO 2026-05-13T23:59:59]` |
| "May 12 to May 13" | `[2026-05-12T00:00:00 TO 2026-05-13T23:59:59]` (assume current year) |
| "since 9am today" | `[<today 09:00:00> TO <now>]` |

Ambiguous formats (e.g. `5/12/2026` — US vs EU): ask the user before guessing.

### Fallbacks

- On **400 `query_syntax_error`**, follow the canonical 400-recovery procedure in the Universal time-range rules section above (strip `Z` → convert `NOW` to absolute → fix field name → only then client-side filter). Never drop the time predicate as your first recovery step.
- If `hostname:<X>` returns 0 results, try `hostname:*<X>*` (substring) once. Hosts in enterprise environments often carry naming prefixes (e.g. a host the user calls "backup" may actually be `srv-backup-01`, `corp-backup-prod`, or have a site/role prefix).
- If both 0, ask the user for the exact hostname rather than guessing further.

### Don't use POST

`POST /api/v2/alert` with a `criteria` body returns 200 on this build but silently ignores the filter (a request scoped to "yesterday" returns multi-year-old records). Never POST for alert search.

### Client-side time filter (when server-side fails)

```
import datetime
now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
cutoff = now - datetime.timedelta(hours=N)
in_window = [r for r in results
             if datetime.datetime.fromisoformat(r["created_time"].rstrip("Z").rstrip("0").rstrip("."))
                >= cutoff]
```

If `in_window` is empty and the newest result is older than the cutoff: report "no alerts in the last N hours" — don't pad with off-window rows.

## Endpoint catalog (top of the iceberg — full list in references/cb-edr-deep-reference.md)

### Alerts
| Method | Path | Use |
|--------|------|-----|
| GET | `/api/v2/alert` | Alert search (canonical — see recipes above) |
| GET | `/api/v1/alert/{alert_id}` | Alert detail by ID |

**Do NOT call** `/api/v3/alert/_search` — confirmed 404 on this build.

### Processes
| Method | Path | Use |
|--------|------|-----|
| GET | `/api/v1/process` | Process search. Time field is `last_update` (NOT `created_time`). |
| GET | `/api/v1/process/{guid}/0` | Process detail |
| GET | `/api/v1/process/{guid}/0/event` | Combined child events |
| GET | `/api/v1/process/{guid}/0/{modload,filemod,netconn,regmod,childproc,crossproc}` | Specific event types |

**Do NOT call** `/api/v2/process` — confirmed 404 on this build (only v1 exists). This is the most common LLM-default mistake; see the version-pin warning in the universal rules section.

Process example (target: last 6 hours of `sshd` on host `web01`, computed at 2026-05-15T15:00:00):
```bash
python3 <cli_path> http --method GET --path /api/v1/process \
  --query q="process_name:sshd AND hostname:web01 AND last_update:[2026-05-15T09:00:00 TO 2026-05-15T15:00:00]" \
  --query rows=5 --query sort="last_update desc"
```

All four universal rules apply: absolute timestamps, no `Z`, mandatory `sort=last_update desc`, mandatory time predicate. `NOW-6HOURS` is **not** a valid substitute for the computed `2026-05-15T09:00:00` lower bound — it returns 400 on this build.

### Sensors
| Method | Path | Use |
|--------|------|-----|
| GET | `/api/v1/sensor?hostname=<X>` | Sensor by hostname |
| GET | `/api/v1/sensor/{id}` | Sensor detail (status, last_checkin, isolation) |
| GET | `/api/v1/sensor?groupid=<n>` | All sensors in group |

### Binaries
| Method | Path | Use |
|--------|------|-----|
| GET | `/api/v1/binary?q={query}&rows=5` | Binary search |
| GET | `/api/v1/binary/{md5}` | Binary detail + signing info |

### Response / containment — write, gate on user approval
| Method | Path | Body | Use |
|--------|------|------|-----|
| POST | `/api/v1/sensor/{id}` | `{"network_isolation_enabled": true}` | Isolate sensor |
| POST | `/api/v1/sensor/{id}` | `{"network_isolation_enabled": false}` | Un-isolate sensor |

Never invoke without explicit user approval naming the target sensor + hostname.

## Query syntax — minimal essentials

| Field | Example | Notes |
|-------|---------|-------|
| `hostname` | `hostname:<X>` (exact) or `hostname:*<X>*` (substring) | |
| `process_name` | `process_name:<exe>` | **Binary basename of the actual kernel exec, NOT argv[0] / script name.** See "Finding scripts" below. |
| `cmdline` | `cmdline:*<token>*` | Substring search over the full command line. Use this for script names, flag values, arguments. |
| `process_md5` | `process_md5:<32-hex>` | |
| `username` | `username:<DOMAIN>\\<user>` (escape backslash) | |
| `severity` | `severity:critical` | Alerts only. |

Booleans uppercase: `AND` `OR` `NOT`. Parentheses for grouping.

### Finding scripts (the `process_name` vs `cmdline` distinction)

CBR's `process_name` field is the basename of the **kernel-level executable**, not the script being interpreted. Concretely:

| Invocation | `process_name` | `cmdline` |
|-----------|----------------|-----------|
| `python3 /tmp/lazagne.py` | `python3` | `python3 /tmp/lazagne.py` |
| `bash /opt/foo/deploy.sh` | `bash` | `bash /opt/foo/deploy.sh` |
| `perl /usr/local/bin/audit.pl --dry-run` | `perl` | `perl /usr/local/bin/audit.pl --dry-run` |
| `powershell.exe -File C:\evil.ps1` | `powershell.exe` | `powershell.exe -File C:\evil.ps1` |

Recipes:

| User intent | Call |
|-------------|------|
| Find script `foo.py` (or any `.py`/`.sh`/`.ps1`/`.rb`/`.pl`/etc.) | `q=cmdline:*foo*` — never `process_name:foo.py` |
| Find any python script running on host `<X>` | `q=process_name:python* AND hostname:<X>` |
| Find binary `nmap` running on host `<X>` | `q=process_name:nmap AND hostname:<X>` — `process_name` is fine for compiled binaries |

**0-result fallback for scripts:** If `process_name:<X>` returns 0 AND `<X>` has a script-language extension (`.py` / `.sh` / `.ps1` / `.rb` / `.pl` / `.js`), **retry once as `cmdline:*<X without extension>*`** before reporting "none found". Don't conclude scripts don't exist just because their interpreter-basename query was empty.

For URL encoding, wildcards, path-escaping, and the full Solr field list: see [references/cb-edr-deep-reference.md](references/cb-edr-deep-reference.md).

## Rendering pattern

Scannable tables, default columns:

- **Alerts:** created_time · severity · hostname · process_name · reason
- **Processes:** last_update · hostname · username · process_name · cmdline (truncate at 80) · childproc_count
- **Sensors:** id · hostname · status · last_checkin_time · network_isolation_enabled
- **Binaries:** md5 · original_filename · digsig_result · digsig_publisher · host_count

≤5 rows → inline. >5 → show 5 + "(N more, ask to see all)". Render once, then drop the raw response.

## Error handling

- **400 `query_syntax_error` on a time-range predicate** — follow the canonical procedure in the Universal time-range rules section: strip `Z` → convert `NOW`/`NOW-NHOUR` to absolute ISO → fix field name → only then client-side filter. Never drop the time predicate as your first recovery step. Most common cause is `NOW` (not accepted on this build) or `Z` suffix.
- **400 `query_syntax_error` on a field name** — `hostname`, `last_update` (processes), `created_time` (alerts) are correct; `host`, `user`, `process_hash`, `start` (on processes), `created_at` are not.
- **401** — `X-Auth-Token` invalid/expired. Stop, ask analyst to re-issue.
- **404 on `/api/info`** — connector points at Carbon Black Cloud, not EDR. Stop and reroute to `cbc-reference`.
- **404 on `/api/v3/alert/_search`, `/api/v2/process`, `/ping`, `/v1/health`** — these endpoints don't exist on this build. Use only the paths in the catalog.
- **404 on `/api/v2/alert/` (trailing slash)** — never include a trailing slash. Use `/api/v2/alert`.
- **0 results for `hostname:<X>`** — retry once with `hostname:*<X>*`; if still 0, ask the user for the exact hostname rather than guessing.
- **TLS handshake error** — set `skip_tls_verification: true` on the connector.

For deeper recovery (alert schema drift table, pivot map, response field reference, worked examples for binaries / containment / per-event-type process drills): see [references/cb-edr-deep-reference.md](references/cb-edr-deep-reference.md).

## Guardrails

- No write actions without explicit user approval naming the target.
- No assumption of `org_key` (single-tenant). If a response includes `org_key`, you're on CBC by mistake — stop, reroute.
- No silent fallback past 401. Surface auth failures.
- Render before pivoting. Answer the ad-hoc question, then offer follow-ups — don't pre-emptively chain.
- Respect the user's scope. "Last 7 hours" means 7 hours, not "latest 25 sorted by time."
