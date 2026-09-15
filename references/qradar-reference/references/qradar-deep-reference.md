# QRadar — Deep Reference (lazy-loaded)

Load this only when SKILL.md sends you here — unknown field, AQL function, full schema, or worked example beyond the canonical offense search.

---

## Pivot map

When a response carries a linkable field, follow it rather than re-searching:

```
Offense ──source_address_ids[]──→ GET /api/siem/source_addresses  (filter: "id IN (4,17)")
                                       └──source_ip──→ AQL WHERE sourceip = '<ip>'
                                       └──source_ip──→ GET /api/asset_model/assets  (filter by IP)

Offense ──id──→ AQL WHERE INOFFENSE(<id>) LAST 7 DAYS  (events)
             └──→ AQL WHERE INOFFENSE(<id>) LAST 7 DAYS  (flows)

Offense ──offense_type──→ GET /api/siem/offense_types
Offense ──closing_reason_id──→ GET /api/siem/offense_closing_reasons

Asset ──ip──→ filter="offense_source = '<ip>'" on /siem/offenses
           └──→ AQL WHERE sourceip = '<ip>'

Log source ──id──→ AQL WHERE logsourceid = <id>  (or LOGSOURCENAME(logsourceid) in SELECT)
```

Carry IDs forward verbatim. If an offense has `source_address_ids: [4, 17]`, resolve both in one call with `filter: "id IN (4, 17)"` — never one call per ID.

---

## AQL functions

| Function | Usage | Returns |
|----------|-------|---------|
| `INOFFENSE(n)` | `WHERE INOFFENSE(42)` | Events/flows correlated to offense ID 42 |
| `INREFERENCESET('set', field)` | `WHERE INREFERENCESET('malicious_ips', sourceip)` | Events where field is in the named reference set |
| `LOGSOURCENAME(logsourceid)` | `SELECT LOGSOURCENAME(logsourceid) AS source` | Log source ID → display name |
| `CATEGORYNAME(category)` | `SELECT CATEGORYNAME(category) AS cat` | Category ID → display name |
| `QIDNAME(qid)` | `SELECT QIDNAME(qid) AS event_name` | QID → event name |
| `REFERENCESETVALUE('set', field)` | `SELECT REFERENCESETVALUE(...)` | Field value metadata from reference set |
| `NOW()` | `WHERE starttime > NOW() - 3600000` | Current epoch ms — prefer `LAST N HOURS` for simple queries |
| `DATEFORMAT(field, 'fmt')` | `SELECT DATEFORMAT(starttime, 'YYYY-MM-dd HH:mm:ss')` | Human-readable timestamp |

**Time range syntax:**
- Simple queries: use `LAST N HOURS` / `LAST N DAYS` — cleaner and validated.
- Queries with `ORDER BY`: use `START <epoch_ms> STOP <epoch_ms>` (unquoted integers) — `ORDER BY` and `LAST` are incompatible.
- `LIMIT` is not supported in AQL query strings. Paginate via `Range: items=0-24` on results fetch.

---

## AQL datasets

### `events`

| Field | Type | Notes |
|-------|------|-------|
| `starttime` | epoch ms | Use in WHERE time filters |
| `endtime` | epoch ms | |
| `sourceip` | string | Source IP (lowercase field name) |
| `destinationip` | string | Destination IP |
| `sourceport` | int | |
| `destinationport` | int | |
| `username` | string | |
| `logsourceid` | int | Resolve via `LOGSOURCENAME()` |
| `logsourcegroupids` | int[] | |
| `category` | int | Resolve via `CATEGORYNAME()` |
| `qid` | int | Resolve via `QIDNAME()` |
| `eventcount` | int | Raw events aggregated into this record |
| `magnitude` | int | 0–10; inherited from associated offense |
| `credibility` | int | 0–10 |
| `relevance` | int | 0–10 |
| `severity` | int | 0–10 |
| `devicetime` | epoch ms | Timestamp from the originating device |
| `payload` | string | Raw log payload — large; omit from SELECT unless needed |
| `protocolid` | int | |
| `domainid` | int | Present in multi-domain deployments |

Do not `SELECT *` on events — `payload` alone can blow the 30 KB spill threshold.

### `flows`

| Field | Type | Notes |
|-------|------|-------|
| `starttime` | epoch ms | |
| `endtime` | epoch ms | |
| `sourceip` | string | |
| `destinationip` | string | |
| `sourceport` | int | |
| `destinationport` | int | |
| `sourcepackets` | long | |
| `destinationpackets` | long | |
| `sourcebytes` | long | |
| `destinationbytes` | long | |
| `protocolid` | int | 6=TCP, 17=UDP, 1=ICMP |
| `applicationid` | int | |
| `firstpackettime` | epoch ms | |
| `lastpackettime` | epoch ms | |

---

## Full offense object schema

`GET /api/siem/offenses/{id}` returns:

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | |
| `description` | string | Auto-generated name |
| `assigned_to` | string | |
| `categories` | string[] | |
| `category_count` | int | |
| `policy_category_count` | int | |
| `security_category_count` | int | |
| `close_time` | epoch ms | Set when CLOSED |
| `closing_user` | string | |
| `closing_reason_id` | int | FK to offense_closing_reasons |
| `credibility` | int | 0–10 |
| `relevance` | int | 0–10 |
| `severity` | int | 0–10 |
| `magnitude` | int | 0–10 |
| `destination_networks` | string[] | |
| `source_network` | string | |
| `device_count` | int | |
| `event_count` | int | |
| `flow_count` | int | |
| `inactive` | bool | True if no new events |
| `last_updated_time` | epoch ms | |
| `last_persisted_time` | epoch ms | |
| `first_persisted_time` | epoch ms | |
| `local_destination_count` | int | |
| `offense_source` | string | Primary source (IP, username, hostname, etc.) |
| `offense_type` | int | Resolve via GET /api/siem/offense_types |
| `protected` | bool | Won't auto-close if true |
| `follow_up` | bool | |
| `remote_destination_count` | int | |
| `source_count` | int | |
| `start_time` | epoch ms | |
| `status` | string | `OPEN` / `HIDDEN` / `CLOSED` |
| `username_count` | int | |
| `source_address_ids` | int[] | Resolve via GET /api/siem/source_addresses |
| `local_destination_address_ids` | int[] | |
| `domain_id` | int | 0 in single-domain deployments |
| `rules` | object[] | `{ id, type }` — type: ADE_RULE / BUILDING_BLOCK_RULE / CRE_RULE |
| `log_sources` | object[] | `{ id, name, type_id, type_name }` |

---

## Reference data API

| Method | Path | Use |
|--------|------|-----|
| GET | `/api/reference_data/sets` | List all reference sets |
| GET | `/api/reference_data/sets/{name}` | Contents of a named set |
| GET | `/api/reference_data/maps` | List all reference maps |
| GET | `/api/reference_data/maps/{name}` | Map contents (key → value) |
| GET | `/api/reference_data/tables` | List all reference tables |
| GET | `/api/reference_data/tables/{name}` | Table contents |

Use `INREFERENCESET('set_name', field)` in AQL to match events server-side rather than fetching the set and filtering client-side.

---

## Analytics rules API

| Method | Path | Use |
|--------|------|-----|
| GET | `/api/analytics/rules` | List CRE rules; filter by `type`, `enabled`, `name` |
| GET | `/api/analytics/rules/{id}` | Rule detail |
| GET | `/api/analytics/building_blocks` | List building block rules |

Rule `type` values: `EVENT` / `FLOW` / `COMMON` / `OFFENSE`.

---

## Worked examples

### Events correlated to offense 56

```json
{
  "method": "POST",
  "path": "/api/ariel/searches",
  "query": {
    "query_expression": "SELECT starttime, sourceip, destinationip, LOGSOURCENAME(logsourceid) AS source, CATEGORYNAME(category) AS category, eventcount FROM events WHERE INOFFENSE(56) LAST 7 DAYS"
  },
  "headers": { "Accept": "application/json", "Version": "14.0" }
}
```

Poll then fetch with `Range: items=0-24`. Renders: starttime · source IP · dest IP · log source · category · event count.

### Resolve source IPs for offense 56

Step 1 — get offense, extract `source_address_ids` (e.g. `[4, 17]`):
```json
{ "method": "GET", "path": "/api/siem/offenses/56", "headers": { "Accept": "application/json", "Version": "14.0" } }
```

Step 2 — resolve all IDs in a single call:
```json
{
  "method": "GET",
  "path": "/api/siem/source_addresses",
  "query": { "filter": "id IN (4, 17)" },
  "headers": { "Accept": "application/json", "Version": "14.0" }
}
```

Never resolve one ID per call — batch with `IN (...)`.

### Top talkers by bytes (last hour, with ORDER BY)

Uses `ORDER BY` — must use `START`/`STOP` (unquoted epoch ms), not `LAST`:

```sql
SELECT sourceip, SUM(sourcebytes) AS total_bytes, COUNT(*) AS flow_count
FROM flows
GROUP BY sourceip
ORDER BY total_bytes DESC
START 1749196400000 STOP 1749200000000
```

Replace `START`/`STOP` values with computed epoch ms at query time.

### Events matching reference set in last 24 hours

```sql
SELECT starttime, sourceip, destinationip, LOGSOURCENAME(logsourceid) AS source
FROM events
WHERE INREFERENCESET('malicious_ips', sourceip)
LAST 24 HOURS
```

Fetch results with `Range: items=0-24`.

---

## Common syntax errors and field drift

| Wrong | Correct | Context |
|-------|---------|---------|
| `severity = 'high'` | `severity >= 7` | Offense filter — severity is numeric |
| `start_time > 1749000000` | `start_time > 1749000000000` | Epoch seconds vs epoch ms |
| `AND` / `OR` lowercase | `AND` / `OR` uppercase | Filter operators must be uppercase |
| `sourceIP` | `sourceip` | AQL fields are lowercase |
| `destinationIP` | `destinationip` | Same |
| `SELECT *` on events | Name specific fields | `payload` blows the 30 KB spill limit |
| Time range omitted | Always include `LAST N HOURS` or `START`/`STOP` | Defaults to last 60 seconds |
| `LIMIT n` in query | Remove — use `Range: items=0-24` on results fetch | AQL returns `Field "LIMIT" does not exist` |
| `SELECT DISTINCT field` | `SELECT field GROUP BY field` | AQL returns `Field "DISTINCT" does not exist` |
| `ORDER BY x LAST 24 HOURS` | `ORDER BY x START <ms> STOP <ms>` | `ORDER BY` and `LAST` are incompatible |
