---
name: cisco-secure-access-ravpn
description: Drive a Cisco Secure Access RA VPN connector via the `cisco_secure_access_ravpn.py` CLI. Fetch historical remote-access VPN (AnyConnect / Secure Client) connect/disconnect/failure events, and list currently active VPN sessions. Walk the catalog into the knowledge graph and emit samples for edge learning. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# Cisco Secure Access RA VPN

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/cisco_secure_access_ravpn.py`. Run `scripts/cisco_secure_access_ravpn.py --help` for the subcommand list; `<subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. **This connector is read-only** -- the underlying `vpn-sessions` resource has a real `PUT` disconnect action on the vendor side, but it is not wired into this CLI. Only two resources exist: `remote-access-events` (historical connect/disconnect/failure events, including the AnyConnect/Secure Client version) and `vpn-sessions` (currently active sessions). No other Secure Access surface (policies, DNS/web reporting, ZTNA, app discovery, DLP) is implemented here -- those would be separate connector types.

## Auth recap

The connector stores two fields -- `api_key`, `api_secret` -- and crogld exchanges them server-side for a bearer token via `https://api.sse.cisco.com/auth/v2/token` (OAuth2 client-credentials grant, **HTTP Basic** `key:secret`, not a body-form client secret), then attaches the `Authorization: Bearer` header automatically on every call. The API key must be a **Standard** key (not a Key Admin key -- those only administer other API credentials and cannot call business endpoints) created with both the `admin.vpn:read` and `reports.aggregations:read` scopes granted. Never ask the user for the API key or secret during analysis, never print them, and never construct the auth header manually.

## Tool call shape

```bash
cisco_secure_access_ravpn.py query --resource remote-access-events --param from=-1days --param to=now --limit 25
cisco_secure_access_ravpn.py query --resource remote-access-events --param from=-1days --param to=now --param connectionevent=failed --limit 25
cisco_secure_access_ravpn.py query --resource vpn-sessions --limit 25
cisco_secure_access_ravpn.py view-result --ref <ref> --row 1
cisco_secure_access_ravpn.py grep --pattern anyconnectversion
cisco_secure_access_ravpn.py ls --path /
```

`--limit` is **required** on `query` -- it is sent verbatim as the API's own `limit` query parameter (no client-side default). It's also code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright, not silently clamped, regardless of either resource's own vendor-side maximum (see per-resource notes below). `--param` is repeatable `KEY=VALUE`; `remote-access-events` requires `from` and `to` (the CLI refuses to run without both). `view-result` requires both `--ref` and `--row`; `--result-set` is optional and defaults to `0`.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `query` (`--resource`, `--param` repeatable, **`--limit` required, hard cap 100**, `--output`) | Fetch one resource. Returns grouped PSV with a `ref` UUID for drill-down. Pass `--output FILE` to also write the rows in the verifiable upload-samples format. |
| `view-result` (`--ref` required, `--row` required, `--result-set` optional) | Drill into one row from a prior query (full entity JSON). Pure cache read. |
| `grep` | String-search this connector's knowledge-graph catalog -- matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags. Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes, not the live API. |
| `upload-samples` | Upload sample rows to the knowledge graph. Takes either `--ref UUID` or `--from-file FILE`, plus the exact `--param` filters (JSON-encoded) used to fetch them as `--query`, and the `--catalog-path` they belong under. Refuses when results exceed a small row cap or the read-before-write check fails. |
| `refresh` | Walk every configured connector's catalog (resource -> field) and commit each tier to the KG. Resumable per-tier; `--max-age` (default `5d`) skips recently-walked subtrees; `--force` re-walks everything. |
| `ls --path /<abs-path> --format json` | List this skill's connectors' schema by UNIX path (Resource -> Field). Fields are discovered empirically from a one-row sample of each resource. |

## Context budget rules

- Default to a small `--limit` (25 is a reasonable starting point). Only raise it on explicit user request, and prefer narrowing `--param` filters first. `--limit` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped, regardless of either resource's own vendor-side maximum.
- For `remote-access-events`, narrow the time window (`from`/`to`) before raising `--limit` -- a wide window over a busy VPN concentrator can return far more events than are useful inline.
- Render the main fields per Rendering pattern below; do not paste full raw JSON for more than a couple of rows inline.
- Drop raw JSON/PSV from reasoning after rendering the answer. Don't re-run an identical `query` already in context -- use `view-result` on the existing `ref` instead.

## Resources

### `remote-access-events` -- `GET /reports/v2/remote-access-events`

Historical connect/disconnect/failure events for remote-access VPN sessions, from the Reporting API. Requires the `reports.aggregations:read` scope.

**Required `--param` filters:**

| Param | Example | Notes |
|---|---|---|
| `from` | `from=-1days` or `from=1619007756000` | Relative time string (`-Ndays`, `-Nminutes`, `-Nweeks`) or epoch milliseconds. |
| `to` | `to=now` or `to=1640010300000` | Same format as `from`. |

**The `to`/`from` window cannot exceed 30 days** -- this is a documented, enforced limit, not just a retention guideline. A wider window returns a `400`; narrow it and retry rather than retrying the same window.

**Optional `--param` filters:**

| Param | Example | Notes |
|---|---|---|
| `ip` | `ip=10.10.10.10` | A single IP address. |
| `identityids` | `identityids=1,2,3` | Comma-delimited identity IDs. |
| `identitytypes` | `identitytypes=network,roaming` | Comma-delimited identity types. |
| `connectionevent` | `connectionevent=failed` | One of `connected`, `disconnected`, `failed`. |
| `anyconnectversions` | `anyconnectversions=4.10.05095,5.10` | Comma-delimited AnyConnect Roaming Security module versions. |
| `osversions` | `osversions=linux-64-Ubuntu 20.04.5 LTS (Focal Fossa)` | Comma-delimited OS version strings. |
| `timezone` | `timezone=ASIA%2fCALCUTTA` | Continent/city, URL-encoded `/`. Display-only; does not filter. |

**Response fields per event:** `osversion`, `internalip`, `connecttimestamp` (epoch seconds), `reason` (e.g. `ACCT_DISC_USER_REQ`), `failedreasons` (array, empty unless the event failed), `connectionevent`, `anyconnectversion`, `publicip`, `vpnprofile`, `sessiontype`, `timestamp` (epoch seconds), `identities` (array of `{id, type: {id, type, label}, label, deleted}` -- `label` is typically the user's email/UPN).

No pagination mechanism is documented for this endpoint beyond `--limit`; if a wide time window returns more rows than `--limit` allows, narrow the window rather than looking for an offset/cursor param -- none exists here. `--limit` is code-enforced at a hard cap of 100 (no vendor-documented maximum for this specific resource, but this connector applies the same ceiling as `vpn-sessions`).

### `vpn-sessions` -- `GET /admin/v2/vpn/userConnections`

Currently *active* VPN sessions, from the Admin API. Requires the `admin.vpn:read` scope. This is a live snapshot, not history -- a session that disconnected a moment ago will not appear.

**Optional `--param` filters:**

| Param | Example | Notes |
|---|---|---|
| `offset` | `offset=100` | Pagination offset; default `0`. |
| `region` | `region=us-east` | Region where the VPN session occurs. |
| `profileName` | `profileName=East coast VPN profile` | VPN profile name. |
| `sortBy` | `sortBy=username` | Only `username` is a supported sort field. |
| `sortOrder` | `sortOrder=desc` | `asc` (default) or `desc`. |
| `usernames` | `usernames=user@example.com,other@example.com` | Comma-delimited email addresses. |

`--limit` maps to the API's own `limit` (vendor max `1000` per call, default `100` if omitted -- but this CLI requires `--limit` explicitly and code-enforces its own lower hard cap of 100, independent of the vendor's higher ceiling). The response also carries `total` and a `cursorId`; this CLI does not auto-paginate past one page -- for more than `--limit` rows, re-run with `--param offset=<N>`.

**Response fields per session:** `username` (email), `deviceName`, `assignedIp` (RFC 1918 IPv4), `assignedIpv6`, `publicIp`, `sessionId`, `loginTime` (ISO 8601), `profileName`.

## Uploading samples

`upload-samples` is provenance-anchored: every uploaded row must be the byte-identical output of a recent `query` issued from this same container. There are two equivalent ways to satisfy it:

1. **`--ref UUID`** -- capture the `Result reference: <uuid>` line that `query` prints on stdout.
2. **`--from-file FILE`** -- when the prior `query` was invoked with `--output FILE`, pass that file.

In either form the **exact same `--param` filters (JSON-encoded, passed as `--query`)** must match what was used for the originating `query`; mismatched values fail the read-before-write check.

The upload cap is **10 rows** (`MAX_UPLOAD_ROWS=10`). `--catalog-path` (required) is the inventory path the sample belongs under, e.g. `/vpn-sessions/deviceName` or `/remote-access-events/connectionevent`.

## Edge-connectable fields

When picking source or target nodes for `kg_learner.py create-edge`, use **scalar identity fields**:

- Identity: `username` (vpn-sessions) or `identities[].label` (remote-access-events) -- both are typically an email/UPN and line up with an identity field on another connector (e.g. Intune's `userPrincipalName`).
- Device: `deviceName` (vpn-sessions).
- Network: `assignedIp`/`publicIp` (vpn-sessions), `internalip`/`publicip` (remote-access-events).

**Do not edge against free prose** or nested arrays as a whole -- the inner-join requires whole-leaf scalar equality, so `identities` (an array of objects) is not itself edge-connectable; only a sampled leaf like `identities[0].label` after flattening is.

## Rendering pattern

For `remote-access-events`, render a compact table/card per event -- don't dump raw PSV/JSON as the answer:

- **User** -- `identities[].label`
- **Event** -- `connectionevent` (connected/disconnected/failed)
- **Reason** -- `reason` (and `failedreasons` if non-empty)
- **AnyConnect version** -- `anyconnectversion`
- **OS** -- `osversion`
- **When** -- `timestamp` or `connecttimestamp`
- **IPs** -- `internalip` / `publicip`

For `vpn-sessions`, render:

- **User** -- `username`
- **Device** -- `deviceName`
- **Profile** -- `profileName`
- **IPs** -- `assignedIp` / `publicIp`
- **Connected since** -- `loginTime`

≤5 rows -> inline table. More than that -> show the first several plus a count of remaining matches, and offer to narrow the filter or time window.

## Error handling

- **400 Bad Request** -- most commonly: `remote-access-events` missing `from`/`to`, a `from`/`to` window wider than 30 days, or an unsupported `sortBy` value on `vpn-sessions` (only `username` is valid). Fix the request; don't retry unchanged.
- **401 Unauthorized** -- bearer token invalid/expired; this should self-heal via the connector's OAuth flow. If it persists, surface that the connector's stored API key/secret may be invalid or revoked -- do not ask the user to paste credentials into chat.
- **403 Forbidden** -- the API key lacks the required scope (`admin.vpn:read` for `vpn-sessions`, `reports.aggregations:read` for `remote-access-events`), or a Key Admin key was used instead of a Standard key. Surface this as a configuration issue for whoever provisioned the connector, not something the agent can work around.
- **404 Not Found** -- unexpected for this CLI's fixed paths; likely indicates the connector's base URL was misconfigured (should be `https://api.sse.cisco.com`).
- **429 Too Many Requests** -- rate limited. Documented limits: `admin` scope (vpn-sessions) is 5 requests/sec, 14/min, 350/30min per API key; the Reporting API (remote-access-events) is 5 requests/sec per API key. Back off and report the rate limit rather than retrying in a loop.
- **5xx** -- upstream Secure Access service issue; report the status and retry only if the user asks.

## Guardrails

- No write actions exist on this connector -- it is read-only. The vendor's `vpn-sessions` resource does support a `PUT` to force-disconnect a session, but that is deliberately not implemented here; do not imply disconnect/remediation is possible through this connector.
- Do not expose or reconstruct the API key or secret.
- Do not fabricate rows when a query returns zero results -- report zero results plainly (the CLI already prints an explicit `No query results (0 rows)` line) and suggest loosening the filter or widening the time window (within the 30-day cap).
- `vpn-sessions` is a live snapshot; don't present it as a historical record, and don't present `remote-access-events` beyond its queried window as if it covers "all" activity -- always note the window that was actually queried.

## Connector base URL

Configure the connector's base URL as the **Secure Access API host**: `https://api.sse.cisco.com`. The CLI sends absolute, per-resource versioned paths (`/reports/v2/remote-access-events`, `/admin/v2/vpn/userConnections`) rather than relying on a version baked into the connector's base URL.

## Required runtime env (set by the container; the CLI errors clearly if missing)

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- the skill auto-resolves its bound connector; the server applies the OAuth Bearer stored under that connector.

## Output format

`query` returns grouped PSV (header `#|`, data rows `<row_num>|`, `|` escaped as `\|`, cells over 1000 chars truncated). Other commands return JSON.
