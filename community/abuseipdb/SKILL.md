---
name: abuseipdb
description: Enrich IP addresses via the `abuseipdb.py` CLI against a community AbuseIPDB connector. Look up an IPv4/IPv6 address and get its AbuseIPDB reputation record (abuse confidence score, total reports, country, ISP, usage type, recent reports), then emit samples to the knowledge graph. Drives a configured connector (APIProxy), resolved automatically. Auth is server-side; no upstream secrets in this container.
version: "0.1.0"
status: draft
---

# AbuseIPDB

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

One CLI: `scripts/abuseipdb.py`. Run `scripts/abuseipdb.py --help` for the subcommand list; `scripts/abuseipdb.py <subcommand> --help` for that subcommand's flags.

This skill is bound to one configured connector and resolves it automatically; you don't pass a connector name. The connector's base URL is the AbuseIPDB v2 API root (`https://api.abuseipdb.com/api/v2`); the server attaches the `Key` header.

AbuseIPDB is an enrichment source, not a searchable log store: you look up one IP address at a time and get its reputation record back. There is no query language and the catalog is shallow (a single `ip_address` IOC type, no field walk) -- the knowledge graph learns AbuseIPDB's shape from uploaded lookup samples, not from a schema crawl.

## Subcommands

| Subcommand | Purpose |
|---|---|
| `abuseipdb.py lookup` | Look up one IP by `--value`; prints the full AbuseIPDB `/check` record as JSON and caches it under a `ref` UUID for drill-down / upload. `--max-age-days` bounds how far back reports are considered (default 90). Pass `--output FILE` to also write it in the verifiable upload-samples format. |
| `abuseipdb.py grep` | String-search this connector's knowledge-graph catalog -- matches `--pattern` against schema-element names, LLM-assigned summaries, and whole tags (exact-token match). Literal substring by default; `--regex` for POSIX regex, `--fuzzy` for typo-tolerant trigram match. Reads persisted KG nodes, not the live API; for cross-connector search use `kg_learner.py grep`. |
| `abuseipdb.py view-result` | Re-print the full JSON record from a prior `lookup`, by `--ref`. |
| `abuseipdb.py upload-samples` | Upload a looked-up record to the knowledge graph. Takes either `--ref UUID` (recent cache) or `--from-file FILE` (a `--output` artifact), plus the exact `--query` used to fetch it, and the `--catalog-path` it belongs under. Refuses when the read-before-write check fails. |
| `abuseipdb.py refresh` | Commit the IOC-type catalog (`ip_address`) to the KG via `commit_walk_tier`. AbuseIPDB exposes no per-type field schema, so the walk stops at the IOC-type tier. Idempotent; `--force` re-commits. |
| `abuseipdb.py ls --path /<abs-path> --format json` | List this skill's AbuseIPDB connectors' database schema by UNIX path (IOC-type only -- AbuseIPDB exposes no per-type field schema). JSON output; consumed by the `kg_learner.py ls` aggregator. For human-friendly Markdown / PSV output, call `kg_learner.py ls` instead. |

## Lookup model

`--value` is an IPv4 or IPv6 address; the lookup calls `GET /check?ipAddress=<value>&maxAgeInDays=<n>` and returns the AbuseIPDB `data` object (`ipAddress`, `abuseConfidenceScore`, `totalReports`, `countryCode`, `isp`, `usageType`, `domain`, `isWhitelisted`, `lastReportedAt`, and -- when present -- a `reports` array).

The provenance identity for a lookup is `"ip_address:<value>"` -- pass that exact string as `--query` to `upload-samples`.

## Uploading samples

`upload-samples` is provenance-anchored: the uploaded record must be the byte-identical output of a recent `lookup` issued from this same container. Either pass `--ref UUID` (the `Result reference:` line `lookup` prints) or `--from-file FILE` (a `lookup --output` artifact). In both forms the exact same `--query` (`ip_address:<value>`) must be passed. `--catalog-path` (required) is the inventory path the record is recorded under -- typically `/ip_address/<value>`.

## Required runtime env

`CROGL_MCP_URL`, `CROGL_CLI_TOKEN`. No upstream secrets -- this skill is bound to one configured connector and resolves it automatically; the server applies the `Key` header stored under that connector.

## Output format

`lookup` and `view-result` print the full record as indented JSON (a single object, not a table). `lookup` appends a trailing `Result reference: <uuid>` line pointing to the drill-down cache.
