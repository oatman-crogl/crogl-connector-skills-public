---
name: runzero
description: Query runZero's read-only Export API via the `runzero.py` CLI -- assets, services, software, vulnerabilities, wireless, sites, certificates, directory users/groups, findings, tasks, subnet utilization, and SNMP ARP cache. Uses a single Export (ET) token, which is structurally read-only (scoped to /api/v1.0/export only). Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# runZero

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

> **v0.1.0** -- 2026-09-09 -- Initial community connector bundle. Docs-only build: every endpoint, query param, response-envelope shape, and search keyword below is sourced from runZero's own help docs (help.runzero.com) and its published OpenAPI spec (github.com/runZeroInc/runzero-api), cross-checked against each other where they disagreed (see Error Handling and per-resource notes). **Not yet live-verified against a real runZero tenant** -- no credentials were available for this build. Treat every claim below as "correct per the vendor's own docs," not "confirmed against a live console."

CLI: [scripts/runzero.py](./scripts/runzero.py). Connector manifest: [connector.json](./connector.json).

This is a **data-retrieval cookbook**: compose the call, render the result, stop. No writes exist on this connector at all -- see Scope Boundary.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. Do not load a separate reference skill first -- this bundle contains the operational call shapes needed to use the CLI.

## Fast Path

```bash
python3 <cli_path> query --resource assets --search 'os:"Windows" AND alive:true' --limit 5
```

## Scope Boundary

runZero's public API has **three token types with genuinely different auth models and privilege levels**, not just different resource domains:

| Token | Prefix | Reachable paths | Capability |
|---|---|---|---|
| **Export** | `ET` | `/api/v1.0/export/*` only | Read-only. This connector. |
| Organization | `OT` | `/api/v1.0/org/*` (+ export) | Read/write within one org: create/edit/delete sites and assets, launch scans. |
| Account | `CT` | `/api/v1.0/account/*` (+ org, export) | Platform admin: create orgs, users, API keys, manage license. |

This connector uses **only** an Export token. That token is issued scoped to one organization and cannot reach any `/org` or `/account` path -- there is no write action to gate here, because the credential is incapable of writes, not because the CLI declines to expose one. Org-level (site/scan/asset CRUD) and account-level (tenant admin) operations are out of scope for this connector type entirely; if a future need arises for those, they belong in a separate connector type with its own `OT`/`CT` credential, not bolted onto this one.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `query` | Search one `--resource` with runZero's search-query syntax. Returns grouped PSV + a result `ref`. |
| `get` | Hydrate one row by `id` (assets, vulnerabilities, wireless, certificates only). |
| `view-result` | Re-print one cached row from a prior query. |
| `refresh`, `ls`, `grep`, `upload-samples` | Standard KG catalog/sample commands. |

## Auth Recap

The connector stores `console_host` (bare host -- `console.runzero.com` for the SaaS platform, or a self-hosted console's FQDN) and `api_token` (secret, an Export/`ET` token). crogld injects the token server-side as `Authorization: Bearer <token>`. The agent never constructs this header or asks the user for the token.

**Self-hosted consoles**: runZero also ships as a self-hosted platform (on-prem or customer cloud), which uses a customer-chosen console hostname and may present a self-signed or internal-CA certificate. Enable **Skip TLS verification** on the connector instance in that case (off by default; the SaaS console at `console.runzero.com` never needs it).

## Context Budget

1. **Default limit** is 5 rows; the CLI sends `page_size` (bounding the response server-side) for the five resources that support it (assets, services, software, vulnerabilities, wireless).
2. **Hard cap is 25 rows**, enforced even if the user says "all"/"everything".
3. **Resources without `page_size` support** (sites, certificates, users, groups, findings, tasks) have **no server-side row cap at all** -- the API always returns its full match set, and the CLI truncates client-side after the fact. A narrow `--search` matters more on these than elsewhere; an unfiltered query on a large org can pull a very large response before truncation even happens. **Re-verified 2026-09-10 against runZero's published OpenAPI spec** (github.com/runZeroInc/runzero-api, field-by-field, not just the prose docs): none of these six endpoints accept `page_size`, `limit`, `per_page`, or any other row-count param -- this is a confirmed vendor API limitation (CONTRIBUTING.md rule 4.4 cannot be satisfied here), not a gap in this CLI. `subnet-utilization` is a small bounded per-subnet aggregate, not a growing per-row table, so it carries little of this risk despite also lacking a limit param. `snmp-arpcache` is the one resource with a real residual risk -- no `search`, no `fields`, no size param of any kind -- and no available request-side mitigation; the hard cap below still bounds what gets cached/rendered after the fetch, which is the only lever available when the vendor exposes none.
4. runZero's own daily rate limit **equals the org's licensed asset count** (see Error Handling) -- treat every call as counting against that budget, not just against latency. Don't re-run an unchanged query within a turn.
5. After rendering the table the user asked for, drop the raw JSON from your reasoning.

## Resources

| `--resource` | `--search` | `--fields` | `page_size` bound | Notes |
|---|---|---|---|---|
| `assets` | yes | yes | yes | Primary asset inventory. Has `id`. |
| `services` | yes | yes | yes | Discovered TCP/UDP/ICMP services. |
| `software` | yes | yes | yes | Installed software instances (thin documented keyword set: `source`, `vendor`, `product`). |
| `vulnerabilities` | yes | yes | yes | Vulnerability instances. Has `id`. |
| `wireless` | yes | yes | yes | Wireless networks/devices. Has `id`. |
| `sites` | yes | yes | no | No `id` keyword documented -- query by `name`. |
| `certificates` | yes | **no** | no | Has `id`, but the export endpoint doesn't take `fields`. |
| `users` | yes | yes | no | Directory users (LDAP/AD/etc. sync), not runZero console users. |
| `groups` | yes | yes | no | Directory groups. |
| `findings` | yes | **no** | no | Grouped risk findings. Identity field is `finding_code`, not `id`. |
| `tasks` | yes | yes | no | Scan/import task history. |
| `subnet-utilization` | **no** | **no** | no | CSV-only (no JSON export exists for this resource at all). Optional `--mask <N>`. |
| `snmp-arpcache` | **no** | **no** | no | CSV-only. No filter params of any kind. |

`get --resource <r> --id <uuid>` only works for `assets`/`vulnerabilities`/`wireless`/`certificates` -- the only resources with a documented `id` search keyword. For everything else, use `query` with the resource's natural identifying field (e.g. `sites` by `name`, `findings` by `finding_code`).

## Query / Filter Syntax

runZero's search-query syntax is shared across every resource (the keyword set differs per resource; see the table below). Verified from `help.runzero.com/docs/search-query-syntax/` and per-resource keyword pages.

- **Boolean logic**: `AND`, `OR`, `NOT`, parenthesized grouping. Example: `os:"Windows 10" AND protocols:http AND NOT protocols:smb2`.
- **Default match is fuzzy.** Use the `=` prefix for exact/prefix/suffix matching, with `%` as a wildcard: `os:="Windows"` (exact), `os:="Ubuntu Linux%"` (prefix). Single-char wildcard is `_`.
- **Empty-value search**: `os:=` finds assets with no identified OS. Single-valued fields only, not multi-value fields like `name`.
- **Time/date fields** support `<`/`>`: `first_seen:<3days`, `last_seen:>2019-08-01`. Relative units: seconds/minutes/hours/months/years, plus the literal `now`.
- **Disambiguation prefixes** `_asset.`/`_service.` exist for keyword collisions between the asset and service inventories -- not needed for the resources this connector queries independently, but relevant if a query pattern is copied from runZero's combined asset+service search UI.
- Quote multi-word values: `name:"web01.corp.lan"`.

### Documented fields by resource (not exhaustive -- see runZero's own `search-query-<resource>` docs for the full list; this is the commonly-useful subset actually verified while building this connector)

**assets**: `os`, `os_version`, `type` (Desktop/Laptop/Server/BMC/Mobile), `hardware`, `name`, `address`, `net` (CIDR), `mac`, `mac_vendor`, `port`, `protocol`, `product`, `first_seen`, `last_seen`, `online`, `os_eol_expired`, `site`, `hosted_zone`, `owner`, `tag`, `comment`, `id`. Foreign-integration attributes use `@<integration>.<source>.<attribute>` (e.g. `@aws.ec2.region:="us-east-2"`, `@crowdstrike.dev.agentVersion`).

**services**: `port`, `tcp`, `udp`, `transport`, `protocol`, `product`, `vhost`, `service_address`, `service_has_public`/`service_has_private`/`service_has_ipv6`, `certificate_cn`, `certificate_self_signed`, `has:<attribute>`.

**software**: `source`, `vendor`, `product`.

**vulnerabilities**: `id`, `source`, `severity`, `severity_score`, `risk`, `risk_score`, `category`, `name`, `cve`, `kev`, `exploitable`, `cvss2_base_score`, `cvss3_base_score`, `epss_score`, `address`, `port`, `finding_code`, `first_detected_at`, `last_detected_at`, `suppressed`.

**wireless**: `ssid`, `bssid`/`mac`, `mac_vendor`, `channel`, `type` (infrastructure/etc.), `encryption`, `authentication`, `signal`, `site`, `id`.

**sites**: `name`, `description`, `scope` (CIDR), `excludes`, `created_at`, `updated_at`.

**certificates**: `id`, `type`, `cn`, `subject`, `issuer`, `valid_from`, `valid_until`, `self_signed`, `is_ca`, `sha256`, `pk_size`, `public_key_insecure`, `signature_algorithm_insecure`, `vulnerability_count`.

**users** (directory): `source`, `name`, `display_name`, `email`, `title`, `location`, `group_id`, `last_logon_at`, `site`, `organization`.

**groups** (directory): `source`, `name`, `display_name`, `user_id`, `email`, `site`, `organization`.

**findings**: `finding_code`, `name`, `description`, `risk`, `category`, `vulnerability_count`, `site_name`, `suppressed`, `last_detected_at`.

**tasks**: `name`, `type`, `status`, `error`, `recur`/`recur_frequency`, `site`, `template_id`, `source`, `created_at`.

## Common Recipes

| User asks | Command |
|---|---|
| "Windows assets that are online" | `query --resource assets --search 'os:"Windows" AND online:true' --limit 5` |
| "Assets vulnerable to Log4Shell" | `query --resource vulnerabilities --search "cve:CVE-2021-44228" --limit 10` |
| "Where is log4j installed" | `query --resource software --search "product:log4j" --limit 10` |
| "Assets with expired OS support" | `query --resource assets --search "os_eol_expired:true" --limit 10` |
| "Certificates expiring soon" | `query --resource certificates --search "valid_until:<30days"` |
| "Self-signed certs" | `query --resource certificates --search "self_signed:true"` |
| "List sites" | `query --resource sites --limit 25` |
| "Recent scan tasks that errored" | `query --resource tasks --search "status:error" --limit 10` |
| "Tell me about asset `<uuid>`" | `get --resource assets --id <uuid>` |
| "Subnet utilization for a /24" | `query --resource subnet-utilization --mask 24` |

## Rendering Pattern

All `query` output is grouped PSV with a `ref`, truncation ≤5 rows inline / >5 shows first 5 + "(N more, ask to see all)" / >50 summarizes stats first. Suggested default columns per resource (trim with `--fields` where supported):

- **assets**: `id` · `name` · `address` · `os` · `type` · `last_seen`.
- **services**: `port` · `protocol` · `product` · `service_address`.
- **vulnerabilities**: `id` · `cve` · `name` · `severity` · `risk_score` · `address`.
- **software**: `vendor` · `product` · `source`.
- **sites**: `name` · `description` · `scope`.
- **certificates**: `cn` · `issuer` · `valid_until` · `self_signed`.

## Error Handling

- **401** -- Export token invalid, revoked, or rotated. Surface and **stop** -- never retry. Advise regenerating the token under Organizations -> [org] -> Edit organization -> Export tokens.
- **404 on any `/export/org/...` path** -- `console_host` is wrong, or (for self-hosted platforms) the console isn't reachable at that host/path.
- **429 / rate limited** -- documented in runZero's own API overview page but **not present as a formal response in the published OpenAPI spec** (the two sources disagreed; the help doc is more likely current). Two independent budgets: a **daily cap equal to the org's licensed asset count**, and a **per-IP cap of 2,000 requests/5 minutes**. Response carries `X-API-Usage-Total`/`X-API-Usage-Today`/`X-API-Usage-Limit`/`X-API-Usage-Remaining` headers -- if available, surface the remaining-quota number rather than just "rate limited." Back off and retry once; don't loop.
- **A resource returning its full unfiltered set when you expected filtering** -- check the Resources table above first. Six resources (`sites`, `certificates`, `users`, `groups`, `findings`, `tasks`) never support server-side `page_size`, and two (`subnet-utilization`, `snmp-arpcache`) support neither `search` nor `fields` at all -- this is a real API limitation (re-verified 2026-09-10 against runZero's published OpenAPI spec), not a CLI bug.
- **Empty result on a valid query** -- a real, correct, empty answer (e.g. no asset matched the filter). The CLI prints an explicit `No <command> results (0 rows) for '<search>'` line, never a blank line.
- **`get` on a resource not in `GET_ID_RESOURCES`** -- the CLI rejects it with a clear message before making a call; use `query --search "id:<value>"` only on `assets`/`vulnerabilities`/`wireless`/`certificates`, and the resource's natural field elsewhere (e.g. `sites --search 'name:"..."'`).

## Guardrails

- **No write actions exist on this connector, full stop.** The Export token cannot reach any mutating endpoint -- there is nothing to gate behind user approval because there is nothing to approve.
- **Never reveal the Export token value.**
- **Respect the user's scope** -- if a query could return data outside the org's intended visibility (e.g. broad `net:` sweeps), prefer the narrowest `--search` that answers the actual question.
- **Don't widen `--limit` past the hard cap** even if asked for "everything" -- narrow the `--search` instead and say so.

## Required Runtime Env

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets are present in the agent container.
