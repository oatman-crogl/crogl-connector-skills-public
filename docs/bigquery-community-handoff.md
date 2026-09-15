# BigQuery Community Connector Handoff

## Summary

Prepared a customer-shareable community connector bundle at `community/bigquery/` so BigQuery can be imported as a connector type instead of relying on branch-local built-in BigQuery skill code.

Status: `draft`
Category: `querying`
Connector type name: `bigquery`

## What was ported from the built-in BigQuery implementation

Ported and adapted from the prior built-in `crogl-bigquery` implementation:

- `query`
- `describe`
- `view-result`
- KG catalog support: `ls`, `refresh`, `grep`, `upload-samples`
- KG edge-learning helpers: `sample`, `lookup`
- Dashboard card producers used by the BigQuery-backed ticket dashboard path:
  - `resolution_time`
  - `open_tickets`
  - `adoption`
- Credential schema and binding model:
  - `project_id` required
  - `default_dataset` optional
  - `service_account_json` optional secret
  - `base_url` bound to `https://bigquery.googleapis.com/bigquery/v2/projects/{{project_id}}`

## What was intentionally omitted or reduced

- No separate BigQuery reference skill; the bundle is self-contained.
- No upstream BigQuery write/mutation commands.
- No customer/demo hostnames, tenant URLs, or internal references.
- No demo-specific dataset allowlist; this version discovers all datasets by default.
- No app/product changes in `croglng`.

## Important remaining dependency outside this repo

The bundle is locally valid, but full dashboard parity still depends on app-side selector logic in `croglng`.

Current mainline `crogl-ui/src/store/selectors/dashboardSelector.ts` only recognizes built-in ticketing connector types such as `crogl-jira` and `crogl-servicenow`. Earlier demo-specific logic accepted a built-in BigQuery connector for the ticket dashboard path. An imported community connector type named `bigquery` will not be auto-selected by that mainline selector unless `croglng` is updated separately.

Implication:

- BigQuery chat/query usage can come from this connector bundle.
- BigQuery-backed dashboard auto-selection is still blocked by mainline app code unless the selector logic is adjusted separately.

## Local validation commands

```bash
crogl validate-connector community/bigquery
(cd community && zip -r ../dist/community/bigquery.zip bigquery -x '*.DS_Store' -x '*.pyc' -x '*__pycache__*')
crogl validate-connector dist/community/bigquery.zip
```

## Import / create commands for later use

From `~/crogl-connector-skills`:

```bash
crogl import-connector-type --file dist/community/bigquery.zip
crogl list-connector-types
```

Overwrite on re-import:

```bash
crogl import-connector-type --file dist/community/bigquery.zip --overwrite
crogl list-connector-types
```

Create the connector instance with the historical dashboard-path name if you want closest behavioral parity:

```bash
crogl create-connector --name ServiceNow-ITSM --type bigquery --value project_id=<gcp-project-id> --value default_dataset=soc_tickets
crogl list-connectors
```

If runtime credentials require the optional JSON secret:

```bash
crogl create-connector --name ServiceNow-ITSM --type bigquery --value project_id=<gcp-project-id> --value default_dataset=soc_tickets --value service_account_json="$(cat /path/to/service-account.json)"
crogl list-connectors
```

## Validation prompts for later use

Schema check:

```text
Use BigQuery to describe the table soc_tickets.incidents and summarize the important columns.
```

Bounded query check:

```text
Use BigQuery to query the 5 most recent rows from soc_tickets.incidents. Show ticket_id, title, severity, assigned_to, created_at, and resolved_at.
```

Dataset discovery check:

```text
Use BigQuery to list the available datasets for this connector and confirm whether soc_tickets is present.
```

Default-dataset check, if `default_dataset=soc_tickets` was set:

```text
Use BigQuery to run: SELECT ticket_id, title, severity, assigned_to, created_at FROM incidents ORDER BY created_at DESC LIMIT 5
```
