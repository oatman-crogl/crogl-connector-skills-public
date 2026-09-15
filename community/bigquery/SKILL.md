---
name: bigquery
description: Query BigQuery through the `bigquery.py` CLI bound to one community connector instance. Run SQL, inspect schemas, refresh KG catalog state, emit samples for knowledge-graph learning, and expose the dashboard card commands used by BigQuery-backed ticket dashboards. Auth stays server-side or via ADC; never ask for raw credentials in chat.
version: "0.1.0"
status: draft
---

# BigQuery

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/bigquery.py`. Run `scripts/bigquery.py --help` for the subcommand list and `scripts/bigquery.py <subcommand> --help` for flags.

This connector bundle is self-contained. Do not import a separate BigQuery reference skill.

## Auth recap

The connector stores:

- `project_id` — required
- `default_dataset` — optional
- `dashboard_tickets_table` — optional, fully-qualified table (dataset.table) for ticket-dashboard cards
- `service_account_json` — optional secret

Expected runtime behavior:

- Prefer Application Default Credentials when the Crogl worker is running on GCE or another environment with ADC available.
- If a service-account JSON secret is configured, keep it server-side; never paste it into chat and never print it.
- The agent should treat this connector as already bound to one configured BigQuery instance and should not ask the user for auth material during normal use.

## Safe fast path

Start with the narrowest read that answers the question:

- Inspect a table shape: `python3 <cli_path> describe --table <dataset.table>`
- Run a bounded query: `python3 <cli_path> query --sql 'SELECT ... FROM <dataset.table> WHERE ...' --limit 20` (`--limit` is code-enforced as `click.IntRange(1, 100)` -- a value above 100 is rejected outright)
- Discover catalog terms already learned into KG: `python3 <cli_path> grep --pattern <term>` or `python3 <cli_path> ls --path /`

Use `refresh` only when KG catalog state is stale or missing. Use `upload-samples` only when you intentionally want to teach the knowledge graph from a query you just ran.

## Core command shapes

| Need | Command |
|---|---|
| Query rows | `python3 <cli_path> query --sql 'SELECT ...' --limit 20` |
| Query with unqualified table names | `python3 <cli_path> query --sql 'SELECT ... FROM alerts' --dataset <dataset> --limit 20` |
| Describe one table | `python3 <cli_path> describe --table <dataset.table>` |
| Direct dataset/table listing | `python3 <cli_path> ls-datasets` or `python3 <cli_path> ls-datasets --dataset <dataset>` |
| KG catalog walk | `python3 <cli_path> ls --path /` |
| KG catalog search | `python3 <cli_path> grep --pattern <term>` |
| Refresh KG catalog | `python3 <cli_path> refresh` |
| Re-open one result row | `python3 <cli_path> view-result --ref <uuid> --result-set <n> --row <n>` |
| Save a verifiable sample artifact | `python3 <cli_path> query --sql 'SELECT ...' --limit 10 --output /tmp/bq-sample.json` |
| Upload verified samples to KG | `python3 <cli_path> upload-samples --ref <uuid> --query 'SELECT ...' --catalog-path /<dataset>/<table>` |
| Dashboard card generation | `python3 <cli_path> dashboard <card> ...` |

## Dashboard-specific behavior

This bundle includes the historical dashboard card producers (`resolution_time`, `open_tickets`, `adoption`) used when ticket-dashboard data is sourced from BigQuery rather than a live ticketing connector.

**Dashboard configuration is customer-specific.** The dashboard cards query a single BigQuery table that holds ticket data. The table name is read from the `BIGQUERY_DASHBOARD_TABLE` environment variable at runtime (typically set from the `dashboard_tickets_table` connector credential field during connector creation). When this variable is not set, dashboard cards degrade to `"unavailable"` rather than querying a non-existent table.

The table must have these columns: `ticket_id`, `created_at`, `resolved_at`, `assigned_to`, `title`, `severity`.

Important boundary: dashboard use still depends on app-side selector logic recognizing the imported connector type. If the app only recognizes built-in ticketing connector types, the connector can expose `dashboard` commands correctly and still not be selected by the UI.

## Rendering guidance

- `query` emits grouped PSV for compact reading and caches a `ref` for drill-down.
- Use `view-result` for one full JSON row instead of re-running a broad query.
- Prefer concise summaries in chat; include raw rows only when the user asks.
- `describe` output is already formatted for humans; do not restate every column unless it matters.

## Error handling

- Missing ADC / bad credential environment: report that BigQuery auth is unavailable in the runtime and stop after one failed attempt.
- Table not found / dataset not found: verify project, dataset, and table qualification before retrying.
- SQL errors: fix the query shape; do not keep retrying the same invalid SQL.
- Empty results are normal; report that plainly.
- If KG commands return nothing, run `refresh` once before concluding the catalog is absent.

## Guardrails

- Default to read-only operations.
- `upload-samples` writes to the Crogl knowledge graph. Use it only when there is a clear KG-learning reason.
- Do not use broad `SELECT *` queries over large tables without a filter or low limit.
- `--limit` (default 50, hard cap 100) is appended as a trailing SQL `LIMIT` only when `--sql` doesn't already contain one. **A `--sql` string with its own explicit `LIMIT` clause is never overridden or capped** -- BigQuery honors whatever the SQL itself declares, regardless of `--limit`. This is a deliberate, documented limitation of exposing a raw-SQL interface, not a bug: don't write (or accept from a user) a `--sql` value with a large hard-coded `LIMIT` as a way to bypass the cap; prefer letting `--limit` add the clause instead.
- Never expose service-account JSON or other credentials.
- Keep examples product-agnostic and customer-shareable.

## Approval language for write-like actions

Before `upload-samples`, confirm intent in plain terms: “This writes representative query rows into Crogl’s knowledge graph for this connector. Proceed?”

No upstream BigQuery mutation commands are included in this first pass.
