---
name: ionic
description: Community connector for the Ionic Data Platform. Execute ad-hoc SQL queries, perform indexed term searches using REGEXP patterns across token types (IPv4, email, filename, CVE, SID, domain, etc.), and inspect catalogs/tables.
version: "0.1.0"
status: draft
---

# Ionic Data Platform Connector

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

The `ionic` connector provides a SQL-based query interface to the Ionic Data Platform, supporting OAuth2-authenticated query execution, indexed term searches, catalog browsing, and schema inspection.

CLI entrypoint: `scripts/ionic.py`

## Authentication & Configuration

The connector supports configuration via environment variables or a JSON configuration file / payload passed via `--config`.

| Configuration Key | Env Var | Kind | Default | Required | Description |
|---|---|---|---|---|---|
| `host` | `IONIC_HOST` | string | — | Yes | Ionic SQL engine / server hostname |
| `port` | `IONIC_PORT` | integer | `443` | No | Ionic server port |
| `auth_realm` | `IONIC_AUTH_REALM` | string | `ionic` | No | Authentication realm name |
| `catalog` | `IONIC_CATALOG` | string | `ionic` | No | Default catalog |
| `schema` | `IONIC_SCHEMA` | string | `default` | No | Default schema |
| `auth_url` | `IONIC_AUTH_URL` | string | — | Yes | OAuth2 token endpoint URL |
| `auth_flow` | `IONIC_AUTH_FLOW` | enum | `SERVICE_ACCOUNT` | No | OAuth2 flow (`SERVICE_ACCOUNT`, `DIRECT`, `EXCHANGE`, `IMPERSONATION`, `DEVICE`) |
| `client_id` | `IONIC_CLIENT_ID` | string | — | Yes | OAuth2 client ID |
| `client_secret` | `IONIC_CLIENT_SECRET` | string (secret) | — | Yes | OAuth2 client secret |
| `username` | `IONIC_USERNAME` | string | — | No | Username for direct / impersonation flows |
| `password` | `IONIC_PASSWORD` | string (secret) | — | No | Password for direct user flow |
| `session_username` | `IONIC_SESSION_USERNAME` | string | — | No | Session user identity for query context |
| `ssl_verification` | `IONIC_SSL_VERIFICATION` | enum | `FULL` | No | SSL verification mode (`FULL`, `CA`, `NONE`) |
| `ca_bundle` | `IONIC_CA_BUNDLE` | string | — | No | Custom CA certificate bundle path |
| `certfile` | `IONIC_CERTFILE` / `IONIC_CERT_PATH` | string | — | No | Client TLS certificate path |
| `keyfile` | `IONIC_KEYFILE` / `IONIC_KEY_PATH` | string | — | No | Client TLS key path |
| `keyfile_pw` | `IONIC_KEYFILE_PW` | string (secret) | — | No | Client TLS key password |
| `source` | `IONIC_SOURCE` | string | `crogl` | No | Client source identifier |
| `client_tags` | `IONIC_CLIENT_TAGS` | array | `["ad-hoc", "ad-hoc:crogl"]` | No | Query session client tags |
| `session_properties` | `IONIC_SESSION_PROPERTIES` | object | `{}` | No | Custom session properties |

## Commands

All commands emit a standard JSON execution envelope containing `status`, `data`, and `count`.

### 1. `query`
Executes an ad-hoc SQL query against the Ionic Data Platform.

```bash
python3 scripts/ionic.py query --sql "SELECT * FROM ionic.default.events WHERE event_type = 'LOGIN'" --limit 50
```

Flags:
- `--sql` (required): SQL query statement to execute.
- `--limit` (optional): Maximum number of records to return (default: 100). Code-enforced hard cap of 100 -- a value above that is rejected outright, not silently clamped. Appended as a trailing SQL `LIMIT` only if `--sql` doesn't already contain one; an explicit `LIMIT` in `--sql` is never overridden or capped.
- `--config` (optional): JSON configuration file path or JSON inline string.

### 2. `term_search`
Performs an indexed term search across tokenized indexes using REGEXP matching.

```bash
python3 scripts/ionic.py term_search --term-type IPV4_TOKEN --pattern "^192\.168\." --limit 100
```

Supported Term Types:
- `IPV4_TOKEN`: IPv4 address / subnet tokens
- `EMAIL_USER`: Email username / address tokens
- `FILE_NAME`: File path / name tokens
- `CVE_TOKEN`: Vulnerability CVE identifiers
- `SID_TOKEN`: Security Identifier (SID) tokens
- `ALPHANUM_TOKEN`: General alphanumeric tokens
- `DOMAIN_TOKEN`: Fully qualified domain name tokens

Flags:
- `--term-type` (required): Target token type identifier.
- `--pattern` (required): REGEXP matching pattern.
- `--catalog` (optional): Target catalog override (defaults to configured catalog).
- `--schema` (optional): Target schema override (defaults to configured schema).
- `--limit` (optional): Maximum matching records to return (default: 100). Same code-enforced hard cap of 100 as `query`.
- `--config` (optional): JSON configuration file path or JSON inline string.

### 3. `list_catalogs`
Lists accessible catalogs in the Ionic cluster.

```bash
python3 scripts/ionic.py list_catalogs
```

`SHOW CATALOGS` is a metadata statement, not a `SELECT` -- Trino/Presto's public SQL reference documents that statement family as not accepting a `LIMIT` clause. Ionic's own grammar isn't independently documented, so this is an inference from architectural resemblance (not a directly confirmed Ionic API fact, unlike the vendor-limitation exceptions used elsewhere in this repo), but appending `LIMIT` risked a SQL syntax error rather than a confirmed no-op -- so this command has no `--limit` flag, and results are truncated client-side to 100 rows instead, with `"truncated": true` added to the response envelope if the real result set was larger.

### 4. `list_tables`
Lists tables available in a target catalog and schema.

```bash
python3 scripts/ionic.py list_tables --catalog ionic --schema default
```

Flags:
- `--catalog` (optional): Target catalog (defaults to configured catalog).
- `--schema` (optional): Target schema (defaults to configured schema).

Same reasoning as `list_catalogs` -- `SHOW TABLES` likely doesn't accept a `LIMIT` clause either, by the same inference rather than a directly confirmed Ionic API fact, so results are truncated client-side to 100 rows, with `"truncated": true` added to the envelope if larger.

## Output Envelope

Standard response format:
```json
{
  "status": "success",
  "data": [
    {
      "column_1": "value1",
      "column_2": "value2"
    }
  ],
  "count": 1
}
```

`list_catalogs`/`list_tables` add `"truncated": true` to this envelope when the client-side 100-row cap actually truncated the result (see those commands above). `query`/`term_search` never add this field -- their `--limit` bounds the request itself.

Error response format:
```json
{
  "status": "error",
  "error": "Error description message"
}
```

## Guardrails

- All connector configuration secrets (such as `client_secret` and `password`) must remain server-side and never be printed in plain text logs.
- Default to bounded limits on queries (`--limit`) to prevent overwhelming output buffers. `--limit` on `query`/`term_search` is code-enforced at a hard cap of 100 -- a request above that is rejected, not silently clamped.
- **A `--sql` string with its own explicit `LIMIT` clause is never overridden or capped** by `--limit` -- the engine honors whatever `LIMIT` the SQL itself declares. This is a deliberate, documented limitation of exposing a raw-SQL interface, not a bug: don't write a `--sql` value with a large hard-coded `LIMIT` as a way to bypass the cap.
- **Large-result pagination is not fully verified.** If the underlying engine's `/v1/statement` HTTP response ever carries a `nextUri`-style continuation token (the Trino/Presto convention this platform's SQL surface closely resembles), this client does not follow it -- it prints a stderr warning rather than silently claiming a complete result set, but does not fetch further pages. Treat a query against a very large table as potentially incomplete even when it returns without error; narrow with `WHERE`/`LIMIT` rather than assuming completeness.
- All documentation, scripts, and schemas are product-generic for the Ionic Data Platform.
