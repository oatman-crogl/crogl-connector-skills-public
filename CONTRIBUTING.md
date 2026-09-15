# Contributing

This is a public mirror of Crogl's internal connector-skills repo. Treat every source file and dist zip as customer-shareable and public by definition -- nothing here should ever reference a specific customer, tenant, or internal environment.

## Non-negotiable rules

### 1. No customer-specific content

Allowed:

- Vendor product names, versions, and build identifiers.
- Generic placeholder hostnames (`web01`, `srv-backup-01`, `<host>`).
- Generic placeholder users (`<DOMAIN>\\<user>`).
- Generic placeholder IOCs (`<32-hex>`, `<ip-address>`).
- Generic deployment descriptions (`on-prem CBR 7.9 install`, `enterprise tenant`).

Not allowed:

- Customer names, domains, site codes, real hostnames, usernames, IPs, hashes, or malware samples.
- Customer-specific bug names or POC narration.
- Crogl-internal demo hostnames, tenant URLs, Tines subdomains, Drive links, Slack links, or vault links.
- API tokens, partial secrets, screenshots containing secrets, or copied credential values.

Before opening a PR, scan your diff by hand for anything on the "Not allowed" list above -- there is no single grep pattern that catches every customer/environment name, so this is a manual review step, not an automated gate.

### 2. Technical content must be real

Every claim about API quirks, status codes, field values, broken endpoints, query syntax, or auth behavior must come from vendor docs, a live trace, a reproducible test, or clearly labeled uncertainty. Do not pad skills with plausible but unverified recipes.

### 3. Connector bundles are self-contained

Every `community/<connector-type>/` bundle must be operationally self-contained. Its `SKILL.md` must include the safe fast path, auth recap, core call shapes, error handling, and write-action guardrails needed for normal use. If connector-specific reference material exists, put it in the community bundle's `SKILL.md`; do not ship or import a separate `references/<connector-type>-reference/` skill for the same connector.

### 4. Every list/query command has a code-enforced result-size ceiling

A 2026-09 repo-wide audit found 13 of 25 community connectors let a caller-supplied `--limit` (or a paginated fetch) pull an unbounded number of rows -- in the worst case, a full tenant table in one call. A `HARD_CAP` documented only in `SKILL.md` prose is not a control; it's a suggestion an agent can be talked past. This rule makes the ceiling part of the code.

Every `community/<connector-type>/scripts/<connector-type>.py` command that calls an endpoint returning a list/collection (alerts, incidents, assets, devices, tickets, logs, vulnerabilities, etc. -- not a single-object lookup) must:

1. **Define `DEFAULT_LIMIT` and `HARD_CAP` module-level constants.** `DEFAULT_LIMIT` is a small, vendor-appropriate default (typically 5-25). `HARD_CAP` is the absolute ceiling no request can exceed regardless of what the caller or an "everything" / "all" / "no limit" request asks for (typically 25-100, lower if individual records are large).
2. **Enforce it on the CLI option itself:** `--limit` must be declared `type=click.IntRange(1, HARD_CAP)`. A bare `type=int` limit option is not acceptable -- it's the single pattern that distinguishes every non-compliant connector found in the audit from every compliant one.
3. **If the endpoint paginates** (offset, cursor, `@odata.nextLink`, `nextKey`, etc.), clamp the caller's limit to `HARD_CAP` *before* the loop starts, and make the loop's stopping condition depend on that clamped value -- never on the raw, unclamped input. Also cap the per-page size requested from the vendor (`min(remaining, MAX_PAGE_SIZE)`); don't leave page size to the vendor's default.
4. **Every request to a list-returning endpoint must send some server-side limiting parameter** (`$top`, `sysparm_limit`, `per_page`, SQL/KQL `LIMIT`/`take`, GraphQL `first`, etc.). Never fetch with no limit param and truncate only after the full response arrives -- that still pulls the unbounded payload over the wire and into memory first.
5. **Document the enforced `HARD_CAP` value in `SKILL.md`** so the calling agent knows the real, code-enforced ceiling, not just a recommended default.
6. If you request pagination metadata (`pageInfo`, `hasNextPage`, etc.) you don't intend to follow, don't leave it dead in the query -- either use it to warn the operator that results were truncated below `HARD_CAP`, or drop it from the request.

**Exception: a genuinely unfixable vendor limitation.** Point 4 can't be satisfied when the vendor's own endpoint accepts no limiting parameter at all -- confirmed against the vendor's actual API reference or an independent client implementation, not just inferred from a lack of documentation. When that's the case: still apply points 1, 2, 3, and 5 to whatever the connector *can* control (the CLI's own `--limit`/`HARD_CAP` still bounds what's cached/rendered after the fetch), and document the specific verified limitation in both a code comment and `SKILL.md` -- state what was checked and when, not just "the vendor doesn't support this." Don't invent client-side pagination the vendor's API can't back, and don't silently leave the gap undocumented either. `runzero` (`community/runzero`, 6 of 13 export resources) and Cortex XDR's `list-endpoints` (`community/cortex-xdr`) are the two confirmed instances of this so far.

Before opening a PR, run:

```bash
grep -L 'click.IntRange' community/*/scripts/*.py | xargs grep -l -- '--limit\|type=int'
```

Any file in the output that defines a `--limit`/list-returning option needs rule 4 applied. This is a whole-file triage pass, not exhaustive -- a file that uses `click.IntRange` on some commands but not others (e.g. one compliant `query` command alongside a non-compliant `list-endpoints` command) won't be flagged. Review every list-returning command in the file individually; don't stop once the grep is clean.

This grep is also click-specific: `community/ionic` predates the shared click-based `_lib` framework (plain `argparse`, no MCP `api_proxy`) and enforces its own ceiling via a custom argparse type instead of `click.IntRange` -- it will always appear in this grep's output despite being compliant. Don't assume every hit is unfixed; check the file's actual CLI framework first.

### 5. Every query/filter string built from caller input must escape values and allowlist field names

A 2026-09 audit of the same 25 community connectors found 8 connectors that build a query/filter string by interpolating raw caller input with no escaping or allowlisting. Most (`purview-dlp`, `intune`, `defender`, `windows-update-reports`, `taegis`, `cortex-xdr`, `bigquery`) do it in the shared `sample`/`lookup` subcommand (wired via `samplecmds.add_commands` / `_sample_rows`), splicing raw `--column`/`--value` into a clause -- often in a file whose *main* `query` command safely parameterizes or allowlists the same kind of input, because that path was written once carefully and `_sample_rows` was written separately, less carefully. `ionic` has the same underlying mistake in a different shape: it predates the shared `_lib`/click framework entirely (see rule 4's grep caveat above), and its bespoke `term_search`/`list_tables` commands splice `--term-type`/`--pattern`/`--catalog`/`--schema` straight into SQL.

The worst instance, `purview-dlp`, is worth walking through because fixing it needs more than "escape values, allowlist columns" (points 1-4 below): `_dlp_filter()` wraps the caller's `--filter` in `f"({user_filter}) and {DLP_SERVICE_SOURCE_FILTER}"` to force every request into DLP-only scope. OData's `and` binds tighter than `or` (same as SQL), so a `--filter` value like `x eq 'y') or (true` recombines into `(x eq 'y') or ((true) and (serviceSource eq 'dataLossPrevention'))` -- the left branch alone can match non-DLP records, and the mandatory scope clause is defeated entirely despite being "there." This is why point 5 below is its own rule, not a footnote.

This rule targets **composition** -- a connector building part of a query string around caller input (a `--column`/`--value` pair spliced into a clause, or a mandatory clause ANDed onto a caller-supplied filter) -- not full passthrough of an entire user-owned query (`--sql`, `--jql`, an unwrapped `--filter` handed straight to the vendor as one opaque value with nothing else concatenated onto it). Passthrough is a different, already-documented tradeoff (see each such connector's SKILL.md Guardrails section); this rule is about the parts a connector itself constructs or combines.

Every place a `community/<connector-type>/scripts/<connector-type>.py` builds a query/filter/SQL/KQL/CQL/OData string that includes caller-supplied input must:

1. **Never interpolate a raw CLI value directly into a query/filter string** via f-string, `.format()`, `%`-formatting, or plain concatenation -- this applies to field/column names exactly as much as it applies to values; an unescaped field name is just as exploitable as an unescaped value once it lands inside a filter clause.
2. **Validate field/column names** (from `--column` or equivalent) against an allowlist before use -- either the vendor's own fixed field list, or a conservative identifier regex (e.g. `^[A-Za-z_][A-Za-z0-9_.]*$`) if no fixed list exists. `windows-update-reports`' `_require_scoped_table()` is a real example of regex-allowlisting an identifier (the leading table name) before it reaches a query. Reject anything that doesn't match with a clear `click.BadParameter` -- don't silently pass it through.
3. **Escape every value** for the target query language's actual quoting rules (SQL/KQL/OData/CQL/etc.) using a local `_quote`-style helper -- `qradar.py`'s `_quote()` and `runzero.py`'s equivalent do this for values, and `tanium.py`'s `_gql_arg()` (routes values through `json.dumps()` for GraphQL-safe escaping) is the strongest example in this repo. Note that `qradar`/`runzero`'s own `_sample_rows` still splices the raw field name in unescaped next to that safely-quoted value -- they're cited here for the value-quoting half only; point 2's field-name allowlist is still missing there too. Confirm your escaping matches the vendor's actual grammar (per rule 2 above) rather than assuming quote-doubling is enough -- e.g. verify whether the target language also treats a bare backslash as an escape character before a trailing one can slip through unescaped.
4. **Prefer parameterization or discrete per-field REST params over string composition** wherever the vendor API supports it -- see `tenable-io`'s per-filter query params (used consistently by both its main `query` and its `sample`/`lookup` path) or `cisco-xdr-incidents`'s flat param dict. A hand-built query string is the fallback, not the default.
5. **A mandatory clause ANDed onto a caller-supplied whole-filter value is not made safe by wrapping the caller's fragment in one pair of parens.** A caller can supply their own unbalanced `) or (...` sequence that reopens grouping and recombines with your clause on the wrong side of the boolean logic (see the `purview-dlp` walkthrough above). Either reject filters with unbalanced parens or a top-level boolean operator, or enforce the scope restriction through a mechanism the caller's filter text cannot reach at all (a separate, non-overridable request parameter rather than string concatenation).
6. **Treat the `sample`/`lookup` subcommand with the same scrutiny as the main `query` command.** It is not a lesser-used or internal-only path -- it's exactly where this bug has recurred across the repo, because it's easy to get the primary command right and then reimplement composition by hand, differently and less carefully, for `_sample_rows`.
7. **If the main query path already enforces an allowlist** (e.g. `cortex-xdr`'s `_build_filters()` with its `allowed_fields`/`allowed_operators`), the `sample`/`lookup` path must reuse or match that same allowlist -- don't let two code paths in the same file enforce different rules for the same underlying request. This applies even when the `sample`/`lookup` path builds a structured filter object instead of a string (as `cortex-xdr`'s does) -- a bypassed allowlist is the same bug whether the vulnerable value ends up in a spliced string or an unchecked dict key.

**Triage:** there is no single reliable grep for this rule the way there is for rule 4 -- unescaped interpolation is a semantic pattern, not a lexical one, and a structured-filter-object violation (point 7) won't show up in a string-literal grep at all. Start with `grep -n 'f"' community/*/scripts/*.py | grep -iE 'filter|where| eq |kql|sql|:='` and `grep -n '\.format(' community/*/scripts/*.py` as a starting point, not a finish line -- then separately grep for every `_sample_rows`/`samplecmds.add_commands` definition and read its field-name handling against whatever allowlist (if any) the file's main query command enforces. Check **both** the main query command's composition and the `_sample_rows`/`lookup` path's composition in the same file -- they are very often written independently and drift apart.

## Artifact types

### Reference skill

Location:

```text
references/<vendor>-reference/
├── SKILL.md
└── references/                 # optional deep docs
```

Reference skills are markdown cookbooks only for connectors that do not yet have a community connector type. They do not include `connector.json` or `scripts/`. When a community connector exists, migrate this content into `community/<connector-type>/SKILL.md` and stop importing the standalone reference.

Required frontmatter:

```yaml
---
name: <vendor>-reference
description: "Reference cookbook for <Vendor Product> when no community connector bundle exists yet. Do not import this skill independently once a <connector-type> community connector exists; move this guidance into the bundle SKILL.md. Covers: <surfaces>."
version: "0.1.0"
status: draft
paired_connector_type: <connector-type>
---
```

Required body sections:

1. Changelog block.
2. Supplemental framing.
3. Connector selection.
4. Auth recap.
5. Tool call shape.
6. Context budget rules.
7. Endpoint catalog.
8. Query/filter syntax and valid field values.
9. Common query recipes.
10. Rendering pattern.
11. Error handling.
12. Guardrails.

### Community connector type

Location:

```text
community/<connector-type>/
├── SKILL.md
├── connector.json
└── scripts/
    └── <connector-type>.py
```

Community connector types are importable Crogl connector bundles. They must pass croglng's connector validator.

Rules:

- Folder name, `SKILL.md` `name:`, and connector type key match.
- Type names must not start with the reserved `crogl-` prefix.
- `connector.json` must include `display_name`, `cli_entrypoint`, and `credential_schema.binding`. It must strictly conform to Crogl's schema (supported `kind`s: `string`, `integer`, `float`, `boolean`; no unknown properties like `default`, `enum`, `commands`).
- Binding URLs must use `https` and public hosts, unless using the single supported operator-supplied host token pattern.
- For a connector with an operator-supplied host (on-prem appliances — SIEM/EDR/ticketing behind a VPN), declare a `skip_tls_verification` boolean field (not required) and set `binding.skip_tls_verification_field` so the connect form offers an off-by-default "Skip TLS verification" checkbox for self-signed / hostname-mismatched certs. Follow the built-in Splunk/Elasticsearch shape (`"Skip TLS certificate verification (for self-signed certs)."`). Public CA-signed SaaS endpoints don't need it.
- Secrets belong in `credential_schema.fields` and server-side binding headers, not in Python code.
- The Python CLI must use the injected `_lib` runtime package. Do not commit or zip `_lib`.
- Write actions require explicit user approval language in the connector `SKILL.md`.
- `SKILL.md` frontmatter must include `version:` (semver, quoted string) and `status:`, and the body must carry a changelog for every version bump — either a blockquote block directly under the H1 (see `jira-datacenter`) or a `## Changelog` section (see `taegis`). **`connector.json` must NOT carry a `version` field** — `crogl validate-connector`'s manifest decoder rejects unknown top-level properties (confirmed: adding `"version"` to a `connector.json` fails with `unknown field "version"`); version lives in `SKILL.md` frontmatter only.
- Add a status/version badge line directly under the H1, immediately before any changelog content: `![status](https://img.shields.io/badge/status-<status>-<color>) ![version](https://img.shields.io/badge/version-<version>-blue)`. Escape any hyphen in the status value as `--` (shields.io's label/message separator), e.g. `field-tested` → `field--tested`. Color mapping: `draft`=`lightgrey`, `field-tested`=`yellow`, `customer-validated`=`brightgreen`, `superseded`=`red`.

Validate:

```bash
crogl validate-connector community/<connector-type>
crogl validate-connector dist/community/<connector-type>.zip
```

### Provisioning guide

Location:

```text
docs/provisioning/<connector-type>/
├── provisioning.adoc   # source
└── provisioning.pdf    # built output
```

Write one for every connector type, even when the credential looks as simple as a single customer-supplied API key -- the *shape* of the credential (one field, no OAuth) doesn't tell you whether *obtaining* it is trivial (a key already visible on a profile page) or a real console step gated behind an admin role (an Enterprise Administrator generating a token from a settings menu, say). Mark any step that couldn't be confirmed against a live vendor console as best-guess/TBD rather than omitting the guide.

This is the one artifact type in this repo written for a human, not for the agent -- see [docs/provisioning-guides.md](./docs/provisioning-guides.md) for the full rationale and authoring checklist. `connector.json`'s `credential_schema.fields` stay the source of truth for *what* values are required; the provisioning guide explains *how a human obtains each one*.

## Build zips

Reference skill zip:

```bash
(cd references && zip -r ../dist/references/<skill-name>.zip <skill-name> -x '*.DS_Store' -x '*.pyc' -x '*__pycache__*')
```

Community connector type zip:

```bash
(cd community && zip -r ../dist/community/<connector-type>.zip <connector-type> -x '*.DS_Store' -x '*.pyc' -x '*__pycache__*')
```

Commit source and generated dist zip together.

## Build provisioning guides

Requires the `asciidoctor-pdf` gem (`gem install asciidoctor-pdf`).

```bash
docs/provisioning/assets/adoc2pdf.sh <connector-type>
```

New guide: copy `docs/provisioning/assets/template.adoc` to `docs/provisioning/<connector-type>/provisioning.adoc` and fill in every placeholder.

Commit source and built PDF together.

## Status values

Use only these status values:

- `draft` -- built and locally validated, not yet proven in a live customer or demo workflow.
- `field-tested` -- validated in acmedemo or a field/demo environment with at least one successful query, lookup, or action.
- `customer-validated` -- confirmed working in a real customer environment or customer POC.

Update status in both the README inventory and relevant frontmatter when status changes — and update the badge (see "Community connector type" rules above) to match; a stale badge is worse than no badge.

## Versioning

Applies uniformly to both artifact types — community connector types included, not just reference skills.

- Patch: typo fixes, wording, no behavior change.
- Minor: new recipes, endpoints, fallback procedures, or connector commands.
- Major: breaking changes, removed recipes, renamed connector type, or changed credential schema.

Prepend changelog notes in `SKILL.md`; do not squash old notes. Bump the version badge in the same commit as the frontmatter `version:` — see "Community connector type" rules above for the badge format.
