# CB-EDR Deep Reference (lazy-loaded)

Load this only when SKILL.md sends you here — when a query hits a field you don't recognize, when you need to pivot across surfaces, or when you need a worked example beyond the canonical alert search.

## Pivot map

```
Alert ───process_guid──→ Process detail (/api/v1/process/{guid}/0)
  │
  └──hostname──→ Sensor (/api/v1/sensor?hostname=…)
            └──→ All processes on host (/api/v1/process?q=hostname:…)
            └──→ All alerts on host (/api/v2/alert?q=hostname:…)

Process ───parent_unique_id──→ Parent process (/api/v1/process/{parent_unique_id}/0)
   │
   ├──hostname──→ Sensor + all-host activity
   ├──username──→ All processes by user (/api/v1/process?q=username:…)
   ├──process_md5──→ Binary detail (/api/v1/binary/{md5})
   │             └──→ Where else seen (/api/v1/process?q=process_md5:…)
   └──process_guid──→ Children (/api/v1/process/{guid}/0/childproc)
                  └──→ modload, filemod, netconn, regmod, crossproc

Binary ───md5──→ Process occurrences (/api/v1/process?q=process_md5:…)
   │        └──→ Modload occurrences (/api/v1/process?q=modload_md5:…)
   └──signing_data──→ Same-signer binaries (/api/v1/binary?q=signing_data.publisher:…)

Sensor ───id/hostname──→ Recent processes (sort=last_update desc)
   └──group_id──→ All sensors in group
```

**Rule:** if a response carries `process_guid`, `hostname`, `username`, `process_md5`, or `sensor_id`, you can pivot from that value into another query. Carry the value forward verbatim.

## Query syntax — extended

### Wildcards
- Trailing wildcard OK: `process_name:laz*`
- Leading wildcard often blocked at the index: `path:*\\\\temp\\\\*` — use sparingly.

### Path / backslash escaping
- Windows paths in Solr: backslashes doubled. `path:c\:\\\\users\\\\*\\\\temp\\\\*`
- Windows usernames: `username:<DOMAIN>\\<user>` — single backslash silently breaks the query.

### URL encoding (the connector handles most of this — passed via `query` field)
- Spaces → `%20` (or just include literal space; the connector URL-encodes the `query` map)
- Brackets `[ ]` → `%5B` / `%5D`
- Double-quotes → `%22`

### Time-range fields by endpoint

| Endpoint | Time field | Syntax (CBR 7.9) |
|----------|-----------|------------------|
| `/api/v2/alert` | `created_time` | `created_time:[2026-05-13T00:00:00 TO 2026-05-14T00:00:00]` — **no `Z` suffix** |
| `/api/v1/process` | `last_update`, `start` | `last_update:[2026-05-12T00:00:00 TO 2026-05-13T00:00:00]` — **no `Z` suffix** |
| `/api/v1/binary` | n/a | use `host_count` / signing fields |

**The `Z` suffix breaks Solr range queries on CBR 7.9.0.251016.0400.** With `Z`: `400 query_syntax_error`. Without `Z`: `200`. Confirmed via trace analysis on an on-prem 7.9 install.

### Common drift (returns 400)
| Wrong | Correct | Endpoint |
|-------|---------|----------|
| `[...Z TO ...Z]` | `[... TO ...]` (no Z) | both — CBR 7.9 Solr quirk |
| `created_at` | `created_time` | alerts |
| `process_hash` | `process_md5` | alerts + processes |
| `host` | `hostname` | alerts + processes |
| `user` | `username` | alerts + processes |

## Response field reference

### Process response (`/api/v1/process`)
| Field | Type | Use |
|-------|------|-----|
| `id` / `unique_id` / `process_guid` | string | Process identity — primary pivot key |
| `process_name` | string | Image filename |
| `process_md5`, `process_sha256` | string | File hashes |
| `hostname` | string | Host the process ran on |
| `username` | string | User the process ran as |
| `cmdline` | string | Full command line |
| `path` | string | Disk path |
| `parent_name`, `parent_unique_id`, `parent_md5` | string | Parent process |
| `start`, `last_update` | ISO 8601 | Process lifetime |
| `interface_ip`, `comms_ip` | string | Sensor IP at event time |
| `childproc_count`, `modload_count`, `filemod_count`, `netconn_count`, `regmod_count`, `crossproc_count` | int | Activity counts |
| `os_type` | string | windows / linux / mac |
| `sensor_id` | int | FK to sensor |

### Alert response (varies by build)
| Field | Use |
|-------|-----|
| `id` / `alert_id` | Alert identity |
| `severity` | low / medium / high / critical |
| `created_time`, `last_update_time` | Timing (response only — not queryable in `q=`) |
| `hostname`, `sensor_id` | Host attribution |
| `process_name`, `process_guid` | Process pivot key |
| `reason`, `threat_id`, `tags` | Description / classification |
| `status` | new / in_progress / resolved |

### Sensor response (`/api/v1/sensor`)
| Field | Use |
|-------|-----|
| `id` | Sensor identity |
| `computer_name` / `hostname` | Hostname |
| `status` | Online / Offline |
| `last_checkin_time` | Liveness — flag if > 24h |
| `network_isolation_enabled` | Boolean |
| `group_id` | FK to sensor group |
| `os_environment_display_string` | OS detail |

### Binary response (`/api/v1/binary/{md5}`)
| Field | Use |
|-------|-----|
| `md5`, `sha256` | Identity |
| `original_filename` | Filename hint |
| `digsig_result` | "Signed" / "Unsigned" / "Bad Signature" / "Invalid Chain" — first triage signal |
| `digsig_publisher`, `digsig_issuer`, `digsig_subject` | Code-signing identity |
| `digsig_sign_time` | When signed |
| `host_count`, `endpoint_count` | Fleet prevalence |
| `observed_filename` | Filenames seen on disk for this hash |

## Worked examples beyond the canonical alert search

### Find every process across the fleet with hash `<X>`
```
GET /api/v1/process?q=process_md5:<X>&rows=10&sort=last_update desc
```
Render (hostname, username, start, cmdline, sensor_id). Group by hostname for fleet view.

### What did process `<guid>` spawn?
```
GET /api/v1/process/<guid>/0/childproc
```
Or for full child events: `GET /api/v1/process/<guid>/0/event`.

### All processes by a specific user in the last day
```
GET /api/v1/process?q=username:<DOMAIN>\\<user> AND last_update:[2026-05-13T00:00:00 TO 2026-05-14T00:00:00]&rows=10&sort=last_update desc
```
Note: `\\` — single backslash breaks the query. No `Z` on timestamps — see SKILL.md.

### Find a hash anywhere — processes OR module loads
Two queries:
```
GET /api/v1/process?q=process_md5:<X> AND last_update:[<start> TO <end>]&rows=5&sort=last_update desc
GET /api/v1/process?q=modload_md5:<X> AND last_update:[<start> TO <end>]&rows=5&sort=last_update desc
```
Dedupe by `process_guid` after merging.

### Which sensors haven't checked in for 24h?
```
GET /api/v1/sensor
```
Client-side filter `last_checkin_time < NOW-1DAY`. No server-side `?last_checkin_lt=` on this build.

### Is hash `<X>` signed?
```
GET /api/v1/binary/<X>
```
Surface `digsig_result`, `digsig_publisher`, `digsig_sign_time`, `original_filename`, `host_count`.

### Recent process tree on host `<X>` (no specific alert)
```
GET /api/v1/process?q=hostname:<X>&rows=5&sort=last_update desc
```
Pick the most suspicious by `childproc_count` or `crossproc_count`; drill via `/api/v1/process/{guid}/0/event`.

## Multi-step / pivot-driven queries

When a question implies a pivot ("investigate alerts on host X — multiple alerts, each potentially needing a process drill"):

1. Fetch the list.
2. Render the list.
3. **STOP and ask which to drill into.** Don't pre-emptively drill all of them.

Exception: if the list is ≤3 and the phrasing implies depth ("investigate", "deep dive"), drill each.

## Build / versioning notes

Targeted CB version: CBR 7.8+ (Broadcom-documented baseline), validated against on-prem CBR 7.9.0.251016.0400 via trace analysis.
- Alert search via Solr `q=`: works for hostname, severity, process_name, and time predicates **as long as timestamps omit the `Z` suffix**.
- POST `/api/v2/alert` with `criteria` body returns 200 but silently ignores the filter — do not use.
- Process search via Solr `q=`: same — time predicates work without `Z`, return 400 with `Z`.
- Endpoints that 404 on this build: `/api/v3/alert/_search`, `/api/v2/process`, `/ping`, `/v1/health`.
- Older builds (≤6.x) may not have `last_update_time` on alerts; the no-Z quirk is specific to CBR 7.9's Solr version.
