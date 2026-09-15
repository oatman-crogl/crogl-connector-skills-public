# Migrating References Into Community Connector Packages

This repo now separates two concepts that were previously blended together:

- **Community connector bundles** under `community/` own the runtime CLI, credential binding, and operational usage guidance.
- **Standalone reference skills** under `references/` are only for connectors that do not yet have a community connector type.

Use this guide when converting an existing markdown-only reference skill into a customer-shareable community connector package.

## Target shape

For an existing reference skill:

```text
references/qradar-reference/
└── SKILL.md
```

Move its operational cookbook into a connector type:

```text
community/qradar/
├── SKILL.md
├── connector.json
└── scripts/qradar.py
```

After migration, the community connector bundle is the analyst cookbook and runtime package. Do not import the old standalone reference independently; it competes with the connector instance skill Crogl creates from the community bundle.

## Migration steps

1. **Classify the connector.** Pick `querying`, `ticketing`, `lookups`, or `generic`.
2. **Choose a built-in template from `croglng`.** Use `crogl-splunk`/`crogl-databricks` for querying, `crogl-jira`/`crogl-servicenow` for ticketing, and `crogl-virustotal` for lookups.
3. **Create `community/<type>/connector.json`.** Define `display_name`, `cli_entrypoint`, credential fields, and HTTPS/public-host binding.
4. **Create `community/<type>/scripts/<type>.py`.** Implement high-level commands with Click and `proxy.dispatch()` through the injected `_lib` runtime.
5. **Create `community/<type>/SKILL.md`.** Move the reference skill's fast path, auth recap, endpoint recipes, rendering guidance, error handling, and guardrails into this file. The bundle must be self-contained for normal operation.
6. **Retire or mark the old reference as pre-community.** Do not build/upload a standalone `dist/references/<type>-reference.zip` for the same connector after the community bundle exists.
7. **Validate and zip.** Use `crogl validate-connector` and build `dist/community/<type>.zip`.
8. **Field-test it.** Import into a live Crogl environment, create a connector instance, and confirm at least one real query/lookup/action succeeds before promoting past `draft`.

## `_lib` rule

Do not commit or zip `_lib`. Crogl injects `_lib` into connector-owned skills at import/runtime. Standalone reference skills do not receive `_lib`, which is why they cannot replace the connector bundle once a typed connector exists.

## Knowledge Graph support

Add KG support when the connector has meaningful schema/sample data:

- A queryable hierarchy such as datasets/tables/fields, indexes/sourcetypes/fields, projects/issue-types/fields.
- Identity-bearing fields that can relate to other connectors.
- Investigation workflows where schema browsing or edge learning adds value.

KG support usually requires:

- A `SchemaProvider` class.
- `discover_children()` for live schema walk.
- `kgcommands.add_commands()` for `ls`, `refresh`, `grep`, and `upload-samples`.
- Optional sampling/lookup commands when edge learning needs representative rows.

Do not force KG into workflow-only or write-only platforms when there is no useful schema to walk.

## Build commands

```bash
crogl validate-connector community/<type>
(cd community && zip -r ../dist/community/<type>.zip <type> -x '*.DS_Store' -x '*.pyc' -x '*__pycache__*')
crogl validate-connector dist/community/<type>.zip
```

## Status promotion

Keep migrated packages at `draft` until live validation passes. Promote to `field-tested` only after acmedemo or a field/demo environment successfully imports the connector type, creates an instance, and completes one representative operation.
