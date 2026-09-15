---
name: solarwinds-service-desk
description: Drive SolarWinds Service Desk / Samanage incidents via the `solarwinds_service_desk.py` CLI. Search, fetch, create, and update incidents with API-token auth injected server-side. Comment write is exposed as best-effort because the token API did not accept the UI comments endpoint in trial testing.
version: "0.1.0"
status: draft
---

# SolarWinds Service Desk

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/solarwinds_service_desk.py`. Run `scripts/solarwinds_service_desk.py --help` for the subcommand list.

This skill is bound to one configured connector and resolves it automatically. Configure the connector host as the API host, usually `api.samanage.com`; crogld injects `X-Samanage-Authorization: Bearer <token>` server-side.

## Fast Path

For a simple incident list request, run the CLI directly:

```bash
python3 <cli_path> search-incidents --per-page 5
```

Do not load a separate reference skill first. This connector bundle contains the operational call shapes needed to use the CLI.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `search-incidents` | List incidents from `/incidents.json`, with paging and optional `--param key=value` filters. Returns grouped PSV and a result `ref`. |
| `get-incident` | Fetch one incident by numeric ID. Use `--layout long` to include comments/attachments when the API returns them. |
| `create-incident` | Create a new incident with `--name`, `--description`, optional requester/priority/state/category fields, and optional extra `--field key=value`. |
| `update-incident` | Update an incident by numeric ID using explicit options or extra `--field key=value`. |
| `add-comment` | Best-effort `POST /incidents/{id}/comments.json`; trial testing showed this may be unavailable through token API auth. |
| `view-result` | Re-print one cached row from a prior search. |
| `refresh`, `ls`, `grep`, `upload-samples` | Standard KG catalog/sample commands. |

## Tested API Behavior

Trial validation on 2026-07-14 confirmed:

- `GET /api.json` with `X-Samanage-Authorization: Bearer <token>` returns 200.
- `GET /incidents.json?per_page=3` returns incidents.
- `GET /incidents/{id}.json?layout=long` returns a single incident.
- `POST /incidents.json` creates a throwaway incident.
- `PUT /incidents/{id}.json` updates incident fields.
- The browser UI posts comments to the tenant host at `/incidents/{id}/comments` using session CSRF. The API-token host returned 401/406 for attempted comment endpoints, so comment writes should be treated as pending until customer/API confirmation.

## Auth Recap

The connector stores one secret field, `api_token`, generated from the SolarWinds Service Desk user setup page. crogld injects it as `X-Samanage-Authorization: Bearer <token>`. The agent must never ask the user to paste the token into chat and must never construct auth headers manually.

Regional API hosts:

- US: `api.samanage.com`
- EU: `apieu.samanage.com`
- AU: `apiau.samanage.com`

## Common Recipes

| User asks | Exact action |
|---|---|
| "Show recent tickets" | `python3 <cli_path> search-incidents --per-page 10` |
| "List 5 recent incidents" | `python3 <cli_path> search-incidents --per-page 5` |
| "Open ticket 123" | `python3 <cli_path> get-incident --id 123 --layout long` |
| "Create a ticket" | Confirm title/requester/description, then use `create-incident`. |
| "Update priority/state" | Confirm target ID, then use `update-incident`. |
| "Comment on a ticket" | Explain that token-auth comment writes are not confirmed; use `add-comment` only in a test tenant or after customer confirmation. |

## Rendering Pattern

`search-incidents` already emits a compact table with ID, number, name, state, priority, requester, assignee, updated time, and comment count. Do not re-query raw JSON unless the user asks for full details.

## Error Handling

- 401/403: token missing, expired, wrong regional host, or insufficient permission.
- 406: wrong Accept header or endpoint not exposed in API version.
- 404: wrong incident ID or unsupported endpoint.
- 5xx: upstream SolarWinds issue; stop after one failure unless asked to retry.

## Guardrails

- Creating or updating incidents is a write action; confirm target and intent first outside test tenants.
- Do not use delete endpoints through this connector.
- Treat `add-comment` as experimental until proven in the target tenant.
- Never reveal API token values.

## Required Runtime Env

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets are present in the agent container.
