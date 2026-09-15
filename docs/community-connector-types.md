# Community Connector Types

Community connector types are customer-shareable connector packages that an admin imports into Crogl. After import, they appear in the connector catalog and can be configured like built-ins.

## Layout

```text
community/<connector-type>/
├── SKILL.md
├── connector.json
└── scripts/
    └── <connector-type>.py
```

## Self-contained bundle

Every connector type in `community/` must be self-contained for normal use. Put connector-specific recipes, auth notes, query syntax, rendering guidance, error handling, and write-action guardrails in `community/<connector-type>/SKILL.md`.

Do not create or import a separate `references/<connector-type>-reference/` skill for the same connector. It competes with the connector instance skill that Crogl creates on import and causes redundant/corrective agent turns.

Correct layout:

```text
community/abuseipdb/
```

## `connector.json`

`connector.json` is server-side metadata. It is parsed by `crogld` with strict unknown-field checking and not delivered as runtime context to the agent container.

Top-level manifest fields:
- `display_name` (string, required)
- `cli_entrypoint` (string, required, relative path under `scripts/`)
- `credential_schema` (object, required)

Credential schema (`credential_schema`):
- `fields` (array of objects): Connect form input definitions.
  - `key` (string, required): Matches `^[a-zA-Z0-9_]+$` (not reserved word `"secret"`).
  - `label` (string, required): Form label text.
  - `description` (string, optional): Input help text.
  - `kind` (string, required): Allowed values are strictly `"string"`, `"integer"`, `"float"`, or `"boolean"`. (Do NOT use `"enum"`, `"array"`, or `"object"`).
  - `secret` (boolean, optional): Set to `true` to mask input and store encrypted.
  - `required` (boolean, optional): Whether input is mandatory.
  *(Note: Unknown field names such as `default`, `enum`, or top-level `commands` are strictly rejected by Crogl's parser).*
- `binding` (object, required): Describes how collected credentials map into requests.
  - `base_url`: Target URL template (`https://...`). Must use `https` and public hosts (or operator-supplied host token).
  - `headers`: Array of static headers with `template` (must have exactly one `{{secret}}`), and `secret_field` or `basic_auth`.
  - `oauth2`: Optional OAuth2 client credentials config (`token_url`, `client_id`, `client_secret_field`).
  - `auth_provider`: Optional generic provider block (`sigv4`, `session_token`, `gcp_service_account`).
  - `skip_tls_verification_field`: Optional boolean field key for self-signed certificates.

Binding rules:

- `base_url` and any OAuth token URL must use `https`.
- Hosts must be public unless using the supported operator-supplied host token pattern.
- Secrets go in credential fields and server-side header bindings, never in Python code.
- The declared `cli_entrypoint` must exist under `scripts/`.

## Python CLI

The CLI should expose category-appropriate commands:

| Category | Typical commands |
|---|---|
| `lookups` | `lookup`, `view-result`, optional KG commands |
| `querying` | `query`, `view-result`, `refresh`, `ls`, `grep`, `upload-samples` |
| `ticketing` | `search-*`, `get-*`, `add-comment`, optional transition/write commands |
| `generic` | Connector-specific verbs with clear help |

The runtime `_lib` package is injected by croglng. Do not commit or zip `_lib`.

## Validate

From repo root:

```bash
crogl validate-connector community/<connector-type>
```

After building the zip:

```bash
crogl validate-connector dist/community/<connector-type>.zip
```

ERROR findings block import and must be fixed. WARNING findings can import, but should be reviewed before customer sharing.

## Build

```bash
(cd community && zip -r ../dist/community/<connector-type>.zip <connector-type> -x '*.DS_Store' -x '*.pyc' -x '*__pycache__*')
```

## Import

Jason/CS runs import against a Crogl install with admin scope:

```bash
crogl import-connector-type --file dist/community/<connector-type>.zip
crogl list-connector-types
```

Overwrite an existing community type:

```bash
crogl import-connector-type --file dist/community/<connector-type>.zip --overwrite
```

Overwrite does not retro-update existing connector instances. Recreate instances when testing changed runtime behavior.

## Delete

```bash
crogl delete-connector-type --name <connector-type>
crogl delete-connector-type --name <connector-type> --cascade
```

Only community types are deletable. Built-ins are reseeded from the binary.
