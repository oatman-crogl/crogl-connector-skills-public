#!/usr/bin/env python3
"""tanium CLI -- drives a Tanium Cloud custom connector via the MCP api_proxy
tool. Read-only across four Tanium modules: Asset, Comply, Patch, and Threat
Response.

Three call shapes live behind one connector because they share a single host
and a single auth header:

  * Asset (product/endpoint-scoped), Comply, and Patch go through Tanium
    Gateway -- one GraphQL endpoint (`/plugin/products/gateway/graphql`)
    documented at help.tanium.com/bundle/ug_gateway_cloud. Verified against
    that live doc. Note Gateway's `assetProductEndpoints` query (used by
    `query-asset-endpoints`) only returns endpoints that have a specific
    software product installed, resolved from real TDS/sensor telemetry --
    it does NOT return endpoints written via Asset's Import API
    (`assetUpsertEndpoints`), confirmed by live introspection against a real
    tenant with both kinds of records present (2026-07-23).
  * Asset also has its own bulk REST listing (`query-asset-list`), a
    separate, documented public API (`/plugin/products/asset/v1/assets`,
    cursor-paginated) that a third-party integration (Brinqa) already builds
    against. Confirmed live (2026-07-23) that this DOES return Import
    API-sourced records alongside real TDS-registered ones -- the only
    verified read path for that data. Same host, same `session` header auth
    as everything else in this connector.
  * Threat Response has no query/list capability in Gateway at all (only a
    resolve-by-GUID mutation, out of scope for this read-only connector), so
    it goes through the legacy Detect3 REST surface
    (`/plugin/products/detect3/api/v1/alerts`) instead. Those paths are
    NOT from an official Tanium doc -- developer.tanium.com's own
    Threat-Response-alerts page 404s, and Tanium's release notes say
    Threat Response 4.0 changed its API. They're cross-checked against
    Palo Alto's open-source Cortex XSOAR "TaniumThreatResponse" integration
    source instead, which may predate that 4.0 change. Treat this resource
    as best-effort until confirmed against a live tenant (see SKILL.md).

Subcommands:
  query-asset-products        Tanium Asset products (query.assetProducts).
  query-asset-endpoints        Endpoints for an Asset product (query.assetProductEndpoints);
                                real TDS-registered clients only, not Import API records.
  query-asset-list              Every Asset record via Asset's own bulk REST API
                                (GET /plugin/products/asset/v1/assets), including
                                Import API-sourced records.
  query-comply-findings        Compliance/benchmark findings (endpoints.compliance.complianceFindings).
  query-comply-cves            CVE findings, CVSS-version-scoped (endpoints.compliance.cveFindings).
  query-patch-definitions      Patch definitions (query.patchDefinitions).
  query-patch-applicability    Patch applicability via the fixed
                                "Patch - Patch List Applicability" sensor.
  get-patch-deployment         Hydrate one Patch deployment by id (query.patchDeployment).
  query-threat-response-alerts List Threat Response alerts (legacy REST, best-effort).
  get-threat-response-alert    Hydrate one Threat Response alert by id (legacy REST, best-effort).
  view-result                  Drill into one row from a prior query.
  refresh, ls, grep, sample, lookup, upload-samples   Standard KG catalog/sample commands.

The configured connector's base URL is the Tanium Cloud API host
(https://<host>); a Tanium API token is injected server-side as the `session`
header on every call, for both Gateway and the legacy REST surface. This
skill is bound to one configured connector and resolves it automatically;
you don't pass a connector name. There are no write/mutation actions
available through this connector -- it is read-only by design.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import click

from _lib import (
    cache,
    env,
    instance,
    kgcommands,
    mcp,
    proxy,
    queryerror,
    samplecmds,
    samples,
)
from _lib.psv import Options, marshal_grouped

SKILL_NAME = "tanium"
PACKAGE = "tanium"
SEARCH_OPTS = Options(max_cell_width=1000, max_columns=20, max_rows=50)
MAX_RESULT_SETS = 10
VIEW_TOOL_NAME = "tanium.py view-result"

# Result-size ceiling (CONTRIBUTING.md "Every list/query command has a
# code-enforced result-size ceiling"). Applies to every query-* command
# except query-asset-list, which gets its own, higher ceiling below --
# it's this connector's only full-tenant bulk listing; every other Asset
# command is already narrow/scoped (see SKILL.md).
DEFAULT_LIMIT = 25
HARD_CAP = 100

GATEWAY_PATH = "/plugin/products/gateway/graphql"
JSON_HEADERS = {"Content-Type": "application/json"}

# Legacy Detect3 REST surface for Threat Response -- see module docstring for
# why this exists alongside Gateway, and how unverified it is.
THR_ALERTS_PATH = "/plugin/products/detect3/api/v1/alerts"

# Asset's own bulk REST listing -- separate from Gateway, documented public
# API (third-party integrations, e.g. Brinqa, build against it). Cursor-
# paginated via `limit`/`minimumAssetId` request params and a `meta.
# {nextAssetId,endOfReader}` response envelope. See module docstring for why
# this is the only confirmed read path for Import API-sourced Asset records.
ASSET_LIST_PATH = "/plugin/products/asset/v1/assets"
ASSET_LIST_PAGE_SIZE = 500

# query-asset-list pages through this bulk REST listing rather than making
# one bounded request, so it gets its own, higher default/ceiling than the
# standard DEFAULT_LIMIT/HARD_CAP above -- 4 pages at ASSET_LIST_PAGE_SIZE.
ASSET_LIST_DEFAULT_LIMIT = 100
ASSET_LIST_HARD_CAP = 4 * ASSET_LIST_PAGE_SIZE  # 2000

# Patch applicability has no dedicated GraphQL field; it's reached through the
# generic sensor mechanism by this exact display name. Brittle if Tanium ever
# renames the sensor -- there is no typed/discoverable alternative documented.
PATCH_APPLICABILITY_SENSOR = "Patch - Patch List Applicability"

# KG catalog tiers: Resource -> Field. Only resources with an unscoped,
# unfiltered "list a few rows" capability are walkable -- patch-applicability
# (needs a customer-specific --computer-group up front) and patch-deployments
# (get-by-id only, no listing) are deliberately excluded; see SKILL.md.
KG_RESOURCE_NAMES = (
    "asset-products",
    "asset-endpoints",
    "asset-list",
    "comply-findings",
    "comply-cves",
    "patch-definitions",
    "threat-response-alerts",
)


def _mcp_client() -> mcp.MCPClient:
    cfg = env.require(
        {
            "CROGL_MCP_URL": "URL of the MCP server inside the agent container",
            "CROGL_CLI_TOKEN": "container-scoped JWT for the CLI to authenticate to MCP",
        }
    )
    return mcp.MCPClient(cfg["CROGL_MCP_URL"], cfg["CROGL_CLI_TOKEN"])


def _parse_json(body: str, what: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as e:
        raise click.ClickException(f"{what}: non-JSON response: {body[:500]}") from e


# --- GraphQL literal serialization ------------------------------------------
#
# Several Gateway input-object type names (e.g. the exact filter/sort input
# types for patchDefinitions) aren't confirmed against a live schema, so
# arguments are inlined as GraphQL literals directly in the query text rather
# than declared as typed $variables -- that only requires knowing field
# *names* (which the docs do show), not the input object's type name.


class _Enum(str):
    """Marker so `_gql_arg` emits this value as a bare GraphQL enum identifier
    (e.g. `GTE`) instead of a quoted string literal."""


def _gql_arg(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, _Enum):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        parts = [f"{k}: {_gql_arg(v)}" for k, v in value.items() if v is not None]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_gql_arg(v) for v in value) + "]"
    raise TypeError(f"unsupported GraphQL literal type: {type(value)!r}")


# --- shared Gateway filter DSL (Comply, Patch definitions) ------------------
#
# Gateway's own filter shape is `{path, value, op, negated}` (single) or
# `{filters: [...]}` (compound AND). Only `GTE` is confirmed as a real
# FieldFilterOp enum value in the docs (used for date/numeric comparisons);
# other comparison operators (LTE/GT/LT/NE) are NOT implemented here because
# their enum member names aren't confirmed -- guessing wrong would silently
# send an invalid query. Equality (default, no op) and negated-equality
# (`negated: true`, a boolean, not an enum) are both safe and implemented.
# OR-combination is also not implemented: the two Gateway doc pages we found
# disagree on the toggle field's name ("or" in the general Overview page vs.
# "any" in the Comply examples page) -- rather than guess, this connector's
# filter syntax only ever produces AND-combined (default) compound filters.

_FILTER_TERM_RE = re.compile(r"^([^:><!]+)(:|!:|>=)(.*)$")


def _parse_gateway_filter(filter_str: str | None) -> dict[str, Any] | None:
    """Parse `path:value` / `path!:value` / `path>=value` terms, comma-separated
    for AND. Returns a dict ready for `_gql_arg`, or None for no filter."""
    if not filter_str:
        return None
    terms: list[dict[str, Any]] = []
    for raw in filter_str.split(","):
        raw = raw.strip()
        if not raw:
            continue
        m = _FILTER_TERM_RE.match(raw)
        if not m:
            raise click.BadParameter(
                f"invalid filter term {raw!r} -- expected 'path:value', "
                "'path!:value' (negated equality), or 'path>=value'",
                param_hint="--filter",
            )
        path, op, value = m.groups()
        term: dict[str, Any] = {"path": path.strip(), "value": value.strip()}
        if op == "!:":
            term["negated"] = True
        elif op == ">=":
            term["op"] = _Enum("GTE")
        terms.append(term)
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return {"filters": terms}


# --- Gateway (GraphQL) transport ---------------------------------------------


def _gateway_request(
    client: mcp.MCPClient,
    connector: str,
    query_text: str,
    *,
    verb: str,
    candidates_path: list[str] | None,
) -> dict[str, Any]:
    r = proxy.dispatch(
        client,
        connector,
        method="POST",
        path=GATEWAY_PATH,
        headers=JSON_HEADERS,
        body=json.dumps({"query": query_text}),
    )
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="graphql",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            query=query_text,
            response=r,
            candidates_path=candidates_path,
            client=client,
        )
    payload = _parse_json(r.body, verb)
    if not isinstance(payload, dict):
        raise click.ClickException(f"{verb}: unexpected Gateway response shape")
    errors = payload.get("errors")
    if errors:
        raise click.ClickException(
            f"{verb} failed -- Gateway returned GraphQL errors: "
            f"{json.dumps(errors)[:500]}"
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _edges(data: dict[str, Any], field: str) -> list[dict[str, Any]]:
    node = data.get(field) if isinstance(data, dict) else None
    edges = node.get("edges") if isinstance(node, dict) else None
    if not isinstance(edges, list):
        return []
    return [
        e["node"]
        for e in edges
        if isinstance(e, dict) and isinstance(e.get("node"), dict)
    ]


def _page_info(data: dict[str, Any], field: str) -> dict[str, Any] | None:
    node = data.get(field) if isinstance(data, dict) else None
    info = node.get("pageInfo") if isinstance(node, dict) else None
    return info if isinstance(info, dict) else None


def _clamp_limit(limit: int, hard_cap: int = HARD_CAP) -> int:
    """Defense-in-depth clamp. `click.IntRange` on each query-* command's own
    `--limit` already enforces `hard_cap`, but every `_SEARCH_FNS` entry here
    is also reachable directly from the shared `sample`/`lookup` commands
    (injected `_lib` runtime), whose own `--limit` is not IntRange-bounded --
    don't trust that caller to have clamped it first."""
    return min(limit, hard_cap)


def _warn_if_more(
    page_info: dict[str, Any] | None,
    limit: int,
    *,
    what: str,
    narrow_hint: str,
    total: int | None = None,
) -> None:
    """This connector never follows Gateway's cursor to a second page (see
    module docstring on why compound Gateway cursor args aren't guessed at),
    so a query that hits `--limit` and one that hits the real end of the
    result set look identical unless `pageInfo.hasNextPage` is surfaced.
    Printed to stderr so it doesn't pollute the PSV result body. `narrow_hint`
    must name only flags the calling command actually accepts."""
    if not (isinstance(page_info, dict) and page_info.get("hasNextPage")):
        return
    total_note = f" ({total} total match upstream)" if isinstance(total, int) else ""
    click.echo(
        f"Note: more {what} than --limit={limit} are available upstream{total_note}; "
        f"showing the first page only. Raise --limit or narrow {narrow_hint} to see more.",
        err=True,
    )


# --- Asset --------------------------------------------------------------


_ASSET_PRODUCT_FIELDS = """
    vendor
    name
    installation { installedCount usedCount unusedCount pendingUsage }
    usage { usageNotDetected notInstalled baselining limited normal high }
    tracking { state reportingPeriodDays normalMinutesUsedPerDay highMinutesUsedPerDay baselinePeriodDays }
    versions { version installs }
"""


def _asset_products_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
    *,
    vendors: list[str] | None = None,
    search: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    limit = _clamp_limit(limit)
    filter_arg = {
        "vendors": vendors or None,
        "search": search,
        "states": [_Enum(state)] if state else None,
    }
    filter_arg = {k: v for k, v in filter_arg.items() if v is not None}
    args = f"first: {limit}"
    if filter_arg:
        args += f", filter: {_gql_arg(filter_arg)}"
    query = f"""
    query exampleAssetProducts {{
      assetProducts({args}) {{
        edges {{ node {{ {_ASSET_PRODUCT_FIELDS} }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    field = "assetProducts"
    data = _gateway_request(
        client, connector, query, verb="query asset products",
        candidates_path=["asset-products"],
    )
    _warn_if_more(
        _page_info(data, field), limit,
        what="asset products", narrow_hint="--vendor/--search/--state",
    )
    return _edges(data, field)[:limit]


_ASSET_ENDPOINT_FIELDS = """
    id eid computerName computerId serialNumber osPlatform operatingSystem
    servicePack manufacturer ipAddress userName createdAt updatedAt
"""


def _asset_endpoints_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
    *,
    vendor: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> list[dict[str, Any]]:
    limit = _clamp_limit(limit)
    filter_arg = {"vendor": vendor, "name": name, "version": version}
    filter_arg = {k: v for k, v in filter_arg.items() if v is not None}
    args = f"first: {limit}"
    if filter_arg:
        args += f", filter: {_gql_arg(filter_arg)}"
    query = f"""
    query exampleAssetProductEndpoints {{
      assetProductEndpoints({args}) {{
        edges {{ node {{ {_ASSET_ENDPOINT_FIELDS} }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    field = "assetProductEndpoints"
    data = _gateway_request(
        client, connector, query, verb="query asset endpoints",
        candidates_path=["asset-endpoints"],
    )
    _warn_if_more(
        _page_info(data, field), limit,
        what="asset endpoints", narrow_hint="--vendor/--name/--version",
    )
    return _edges(data, field)[:limit]


def _asset_list_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Page through Asset's bulk REST listing (GET .../asset/v1/assets) via
    `minimumAssetId`/`nextAssetId` cursor pagination until `--limit` rows are
    collected or the API reports `endOfReader`. Unlike `assetProductEndpoints`
    (Gateway), this includes Import API-sourced records -- see module
    docstring.

    `limit` is clamped to `ASSET_LIST_HARD_CAP` before the loop starts -- see
    `_clamp_limit`."""
    limit = _clamp_limit(limit, ASSET_LIST_HARD_CAP)
    rows: list[dict[str, Any]] = []
    min_asset_id: int | None = None
    while len(rows) < limit:
        query: dict[str, str] = {"limit": str(min(limit - len(rows), ASSET_LIST_PAGE_SIZE))}
        if min_asset_id is not None:
            query["minimumAssetId"] = str(min_asset_id)
        r = proxy.dispatch(
            client, connector, method="GET", path=ASSET_LIST_PATH, query=query,
        )
        if r.status_code >= 400:
            raise queryerror.actionable(
                interface="rest-key",
                verb="query asset list",
                connector=connector,
                package=PACKAGE,
                response=r,
                candidates_path=["asset-list"],
                client=client,
            )
        payload = _parse_json(r.body, "query asset list")
        if not isinstance(payload, dict):
            raise click.ClickException("query asset list: unexpected response shape")
        page = payload.get("data")
        if not isinstance(page, list) or not page:
            break
        rows.extend(a for a in page if isinstance(a, dict))
        meta = payload.get("meta") or {}
        if meta.get("endOfReader"):
            break
        min_asset_id = meta.get("nextAssetId")
        if min_asset_id is None:
            break
    return rows[:limit]


# --- Comply ---------------------------------------------------------------


_COMPLY_FINDING_FIELDS = (
    "category excepted firstFoundDate id lastScanDate profile profileVersion "
    "rule ruleId standard standardVersion state"
)

# Field sets fork by CVSS version -- these are NOT co-selectable in one
# request. See module docstring / SKILL.md.
_CVE_FIELDS_COMMON = (
    "absoluteFirstFoundDate affectedProducts cisaDateAdded cisaDueDate "
    "cisaNotes cisaProduct cisaRequiredAction cisaShortDescription "
    "cisaVendor cisaVulnerabilityName cpes cveId cveYear detectedProducts "
    "detectedCPEs excepted firstFound isCisaKev lastFound scanType summary"
)
_CVE_FIELDS_BY_VERSION = {
    "v2": f"{_CVE_FIELDS_COMMON} cvssScore severity",
    "v3": f"{_CVE_FIELDS_COMMON} cvssScoreV3 cvssTemporalScoreV3 severityV3",
    "v4": (
        "cveId cveYear cpes detectedProducts detectedCPEs excepted firstFound "
        "lastFound scanType summary affectedProducts cvssScoreV4 "
        "cvssSeverityV4 cvssThreatScoreV4 cvssVectorV4 cwes { id name } "
        "mitreAttacks { id name } alias epssScore epssPercentile "
        "epssPercentileRange maxMaturity createdDate modifiedDate mitreLink "
        "nistLink secpodLink solutionLinks remediation"
    ),
}


def _endpoints_scope_args(limit: int, computer_group: str | None) -> str:
    args = f"first: {limit}"
    if computer_group:
        args += f", filter: {_gql_arg({'memberOf': {'name': computer_group}})}"
    return args


def _comply_findings_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
    *,
    computer_group: str | None = None,
    filter_str: str | None = None,
) -> list[dict[str, Any]]:
    limit = _clamp_limit(limit)
    finding_filter = _parse_gateway_filter(filter_str)
    # Empty parens `fieldName()` are invalid GraphQL syntax -- omit the
    # argument list entirely rather than passing an empty one when there's
    # no filter (caught by the local mock harness, not a live tenant).
    finding_args = f"(filter: {_gql_arg(finding_filter)})" if finding_filter else ""
    args = _endpoints_scope_args(limit, computer_group)
    query = f"""
    query exampleEndpointComplianceFindings {{
      endpoints({args}) {{
        edges {{ node {{ name ipAddress compliance {{
          complianceFindings{finding_args} {{ {_COMPLY_FINDING_FIELDS} }}
        }} }} }}
        pageInfo {{ startCursor endCursor hasPreviousPage hasNextPage }}
      }}
    }}
    """
    field = "endpoints"
    data = _gateway_request(
        client, connector, query, verb="query comply findings",
        candidates_path=["comply-findings"],
    )
    # pageInfo here is over `endpoints`, not findings -- it flags endpoints
    # beyond --limit that weren't scanned at all, not truncated findings on
    # the endpoints that were.
    _warn_if_more(
        _page_info(data, field), limit,
        what="endpoints", narrow_hint="--computer-group or --filter",
    )
    rows: list[dict[str, Any]] = []
    for ep in _edges(data, field):
        compliance = ep.get("compliance") or {}
        findings = compliance.get("complianceFindings") or []
        for f in findings:
            if isinstance(f, dict):
                rows.append(
                    {"endpointName": ep.get("name"), "endpointIpAddress": ep.get("ipAddress"), **f}
                )
    return rows[:limit]


def _comply_cves_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
    *,
    cvss_version: str = "v3",
    computer_group: str | None = None,
    filter_str: str | None = None,
) -> list[dict[str, Any]]:
    if cvss_version not in _CVE_FIELDS_BY_VERSION:
        raise click.BadParameter(
            f"--cvss-version must be one of {sorted(_CVE_FIELDS_BY_VERSION)}",
            param_hint="--cvss-version",
        )
    limit = _clamp_limit(limit)
    finding_filter = _parse_gateway_filter(filter_str)
    finding_args = f"(filter: {_gql_arg(finding_filter)})" if finding_filter else ""
    args = _endpoints_scope_args(limit, computer_group)
    fields = _CVE_FIELDS_BY_VERSION[cvss_version]
    query = f"""
    query exampleEndpointCveFindings {{
      endpoints({args}) {{
        edges {{ node {{ name ipAddress compliance {{
          cveFindings{finding_args} {{ {fields} }}
        }} }} }}
        pageInfo {{ startCursor endCursor hasPreviousPage hasNextPage }}
      }}
    }}
    """
    field = "endpoints"
    data = _gateway_request(
        client, connector, query, verb="query comply CVE findings",
        candidates_path=["comply-cves"],
    )
    # pageInfo here is over `endpoints`, not findings -- see query-comply-findings.
    _warn_if_more(
        _page_info(data, field), limit,
        what="endpoints", narrow_hint="--computer-group or --filter",
    )
    rows: list[dict[str, Any]] = []
    for ep in _edges(data, field):
        compliance = ep.get("compliance") or {}
        findings = compliance.get("cveFindings") or []
        for f in findings:
            if isinstance(f, dict):
                rows.append(
                    {"endpointName": ep.get("name"), "endpointIpAddress": ep.get("ipAddress"), **f}
                )
    return rows[:limit]


# --- Patch ------------------------------------------------------------------


_PATCH_DEFINITION_FIELDS = (
    "id createdDate cveIds isSuperseded platform releaseDate severity "
    "sizeInBytes title"
)


def _patch_definitions_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
    *,
    filter_str: str | None = None,
) -> list[dict[str, Any]]:
    limit = _clamp_limit(limit)
    filt = _parse_gateway_filter(filter_str)
    args = f"first: {limit}"
    if filt:
        args += f", filter: {_gql_arg(filt)}"
    query = f"""
    query examplePatchDefinitions {{
      patchDefinitions({args}) {{
        edges {{ node {{ {_PATCH_DEFINITION_FIELDS} }} }}
        pageInfo {{ hasNextPage hasPreviousPage startCursor endCursor }}
        totalRecords
      }}
    }}
    """
    field = "patchDefinitions"
    data = _gateway_request(
        client, connector, query, verb="query patch definitions",
        candidates_path=["patch-definitions"],
    )
    node = data.get(field) if isinstance(data, dict) else None
    total_records = node.get("totalRecords") if isinstance(node, dict) else None
    _warn_if_more(
        _page_info(data, field), limit,
        what="patch definitions", narrow_hint="--filter",
        total=total_records if isinstance(total_records, int) else None,
    )
    return _edges(data, field)[:limit]


def _patch_applicability_search(
    client: mcp.MCPClient,
    connector: str,
    limit: int,
    *,
    computer_group: str,
) -> list[dict[str, Any]]:
    args = _endpoints_scope_args(limit, computer_group)
    query = f"""
    query examplePatchApplicability {{
      endpoints({args}) {{
        edges {{ node {{
          id name
          sensorReadings(sensors: [{{name: {json.dumps(PATCH_APPLICABILITY_SENSOR)}}}]) {{
            columns {{ name values }}
          }}
        }} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    field = "endpoints"
    data = _gateway_request(
        client, connector, query, verb="query patch applicability",
        candidates_path=None,
    )
    _warn_if_more(
        _page_info(data, field), limit,
        what="endpoints", narrow_hint="--computer-group",
    )
    rows: list[dict[str, Any]] = []
    for ep in _edges(data, field):
        row: dict[str, Any] = {"id": ep.get("id"), "name": ep.get("name")}
        for reading in ep.get("sensorReadings") or []:
            for col in reading.get("columns") or []:
                row[col.get("name")] = col.get("values")
        rows.append(row)
    return rows[:limit]


_PATCH_DEPLOYMENT_FIELDS = """
    id
    author { id displayName username }
    contentDeploymentType
    contentSet { id }
    createdTime
    description
    downloadImmediately
    name
    overrideBlocklists
    patches { id }
    patchLists { id version }
    platform
    restart
    schedule {
      distributeOver { nanoseconds seconds }
      endTime eussAvailableBeforeStart eussHideFromActivity
      overrideMaintenanceWindows startTime timeZone type
    }
    status
    stoppedTime
    targets { computerGroups { id } }
    type
    updatedTime
"""


def _patch_deployment_get(
    client: mcp.MCPClient, connector: str, deployment_id: str
) -> dict[str, Any]:
    query = f"""
    query examplePatchDeployment {{
      patchDeployment(ref: {{id: {json.dumps(deployment_id)}}}) {{
        {_PATCH_DEPLOYMENT_FIELDS}
      }}
    }}
    """
    data = _gateway_request(
        client, connector, query, verb="get patch deployment",
        candidates_path=None,
    )
    result = data.get("patchDeployment")
    if not isinstance(result, dict):
        raise click.ClickException(f"patch deployment {deployment_id!r} not found")
    return result


# --- Threat Response (legacy REST, best-effort -- see SKILL.md) -------------


def _thr_rest_request(
    client: mcp.MCPClient,
    connector: str,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None,
    verb: str,
) -> Any:
    r = proxy.dispatch(client, connector, method=method, path=path, query=query or {})
    if r.status_code >= 400:
        raise queryerror.actionable(
            interface="rest-key",
            verb=verb,
            connector=connector,
            package=PACKAGE,
            response=r,
            candidates_path=["threat-response-alerts"],
            client=client,
        )
    return _parse_json(r.body, verb)


def _threat_response_alerts_search(
    client: mcp.MCPClient, connector: str, limit: int
) -> list[dict[str, Any]]:
    limit = _clamp_limit(limit)
    # `limit` (and `offset`, defaulted to 0 here) is the same collection
    # query-param convention documented for Tanium's other legacy REST
    # module APIs (e.g. Reporting, Connect); not independently confirmed for
    # Detect3's alerts endpoint specifically -- see module docstring on how
    # unverified this transport is. Sent regardless, per CONTRIBUTING.md
    # rule 4: never fetch with no limit param and truncate only client-side.
    payload = _thr_rest_request(
        client, connector, "GET", f"{THR_ALERTS_PATH}/",
        query={"limit": str(limit), "offset": "0"},
        verb="query threat response alerts",
    )
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        # Wrapper key is unconfirmed for this API version -- try the common
        # candidates rather than assuming one.
        rows = next(
            (payload[k] for k in ("data", "alerts", "result") if isinstance(payload.get(k), list)),
            [],
        )
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)][:limit]


def _threat_response_alert_get(
    client: mcp.MCPClient, connector: str, alert_id: str
) -> dict[str, Any]:
    payload = _thr_rest_request(
        client, connector, "GET", f"{THR_ALERTS_PATH}/{alert_id}",
        query=None, verb="get threat response alert",
    )
    if not isinstance(payload, dict):
        raise click.ClickException(f"threat response alert {alert_id!r}: unexpected response shape")
    return payload


# --- KG schema provider ------------------------------------------------------


_SEARCH_FNS = {
    "asset-products": lambda c, conn, limit: _asset_products_search(c, conn, limit),
    "asset-endpoints": lambda c, conn, limit: _asset_endpoints_search(c, conn, limit),
    "asset-list": lambda c, conn, limit: _asset_list_search(c, conn, limit),
    "comply-findings": lambda c, conn, limit: _comply_findings_search(c, conn, limit),
    "comply-cves": lambda c, conn, limit: _comply_cves_search(c, conn, limit),
    "patch-definitions": lambda c, conn, limit: _patch_definitions_search(c, conn, limit),
    "threat-response-alerts": lambda c, conn, limit: _threat_response_alerts_search(c, conn, limit),
}


class _TaniumSchemaProvider:
    """SchemaProvider for Tanium: Resource -> Field, sampled empirically (no
    createmeta-style schema endpoint on either transport)."""

    PACKAGE = PACKAGE
    PLATFORM = "Tanium"
    TIERS_SINGULAR: tuple[str, ...] = ("Resource", "Field")
    TIERS_PLURAL: tuple[str, ...] = ("Resources", "Fields")
    LEAF_DISCOVERY_SAMPLED = True

    def __init__(self, client: mcp.MCPClient) -> None:
        self._client = client

    def list_my_connectors(self) -> list[str]:
        bound = instance.bound_connector_name(self._client)
        return [bound] if bound is not None else []

    def list_children(self, connector: str, tiers: list[str]) -> list[str]:
        return kgcommands.kg_list_children(self._client, connector, tiers)

    def discover_children(self, connector: str, tiers: list[str]) -> list[str]:
        if len(tiers) == 0:
            return list(KG_RESOURCE_NAMES)
        if len(tiers) == 1:
            return self._discover_fields(connector, tiers[0])
        return []

    def _discover_fields(self, connector: str, resource: str) -> list[str]:
        fn = _SEARCH_FNS.get(resource)
        if fn is None:
            return []
        rows = fn(self._client, connector, 1)
        if not rows:
            return []
        return sorted(str(k) for k in rows[0].keys())


# --- CLI ---------------------------------------------------------------


_EXAMPLES = """
Examples:

\b
  tanium.py query-asset-products --search "Chrome" --limit 25
  tanium.py query-asset-list --limit 100
  tanium.py query-comply-cves --cvss-version v3 --computer-group "All Windows" --filter "cveYear:2024" --limit 25
  tanium.py query-patch-definitions --filter "severity:Critical" --limit 25
  tanium.py get-patch-deployment --id <deployment-id>
  tanium.py query-threat-response-alerts --limit 25
  tanium.py view-result --ref <ref> --row 1
"""


@click.group(epilog=_EXAMPLES)
@click.option(
    "--json-meta",
    is_flag=True,
    help="Print a trailing JSON line with run metadata (latency, row count, etc.).",
)
@click.pass_context
def cli(ctx: click.Context, json_meta: bool) -> None:
    """Drive a read-only Tanium connector (Asset, Comply, Patch, Threat Response). Run `<command> --help` for details."""
    ctx.obj = {"json_meta": json_meta}


def _emit_query_result(
    ctx: click.Context,
    *,
    producer_verb: str,
    catalog_path: list[str],
    query_desc: str,
    rows: list[dict[str, Any]],
    output_path: str | None,
    started: float,
    connector: str,
) -> None:
    ref = str(uuid.uuid4())
    producer = f"{SKILL_NAME}/{producer_verb}"

    if not rows:
        click.echo("")
        kgcommands.emit_meta(
            ctx.obj["json_meta"],
            query=query_desc,
            row_count=0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return

    body_text, groups = marshal_grouped(
        rows,
        SEARCH_OPTS,
        max_result_sets=MAX_RESULT_SETS,
        ref=ref,
        view_tool_name=VIEW_TOOL_NAME,
    )
    cache.put(
        groups,
        ref=ref,
        producer=producer,
        connector=connector,
        query=query_desc,
        catalog_path=catalog_path,
    )
    if output_path:
        samples.write_export(
            output_path,
            producer=producer,
            connector=connector,
            query=query_desc,
            ref=ref,
            rows=rows,
        )
    click.echo(body_text)
    kgcommands.emit_meta(
        ctx.obj["json_meta"],
        query=query_desc,
        row_count=len(rows),
        ref=ref,
        output=output_path,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _output_option():
    return click.option(
        "--output",
        "output_path",
        type=click.Path(dir_okay=False, writable=True),
        default=None,
        help="Also write the result rows to FILE in the verifiable upload-samples format.",
    )


def _limit_option(default: int = DEFAULT_LIMIT, hard_cap: int = HARD_CAP):
    return click.option(
        "--limit",
        type=click.IntRange(1, hard_cap),
        default=default,
        show_default=True,
        help=f"Max rows to fetch and render (hard cap {hard_cap}).",
    )


@cli.command(name="query-asset-products")
@click.option("--vendor", "vendors", multiple=True, help="Filter by vendor (repeatable).")
@click.option("--search", default=None, help="Free-text search over product name/vendor.")
@click.option(
    "--state",
    default=None,
    help="Filter by Asset product tracking state (e.g. Cataloged -- the only value confirmed in vendor docs; other states are tenant-defined).",
)
@_limit_option()
@_output_option()
@click.pass_context
def query_asset_products(ctx, vendors, search, state, limit, output_path):
    """List Tanium Asset products (query.assetProducts)."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _asset_products_search(
            client, connector, limit, vendors=list(vendors) or None, search=search, state=state,
        )
    _emit_query_result(
        ctx, producer_verb="query-asset-products", catalog_path=["asset-products"],
        query_desc=f"vendors={list(vendors)} search={search!r} state={state!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="query-asset-endpoints")
@click.option("--vendor", default=None, help="Asset product vendor.")
@click.option("--name", default=None, help="Asset product name.")
@click.option("--version", default=None, help="Asset product version.")
@_limit_option()
@_output_option()
@click.pass_context
def query_asset_endpoints(ctx, vendor, name, version, limit, output_path):
    """List endpoints associated with an Asset product (query.assetProductEndpoints)."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _asset_endpoints_search(
            client, connector, limit, vendor=vendor, name=name, version=version,
        )
    _emit_query_result(
        ctx, producer_verb="query-asset-endpoints", catalog_path=["asset-endpoints"],
        query_desc=f"vendor={vendor!r} name={name!r} version={version!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="query-asset-list")
@_limit_option(default=ASSET_LIST_DEFAULT_LIMIT, hard_cap=ASSET_LIST_HARD_CAP)
@_output_option()
@click.pass_context
def query_asset_list(ctx, limit, output_path):
    """List every Asset record via Asset's own bulk REST API (GET /plugin/products/asset/v1/assets), cursor-paginated by minimumAssetId/nextAssetId.

    Unlike query-asset-endpoints (Gateway's assetProductEndpoints, scoped to
    endpoints with a specific software product installed and resolved from
    real TDS/sensor telemetry only), this REST surface returns every Asset
    record regardless of source -- including endpoints written via the
    Import API (assetUpsertEndpoints), which Gateway's endpoints()/
    assetProductEndpoints queries cannot see at all. Confirmed live
    (2026-07-23) against a real tenant with both real and Import
    API-sourced records present.
    """
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _asset_list_search(client, connector, limit)
    _emit_query_result(
        ctx, producer_verb="query-asset-list", catalog_path=["asset-list"],
        query_desc=f"limit={limit}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


_FILTER_HELP = (
    "Comma-separated AND filter terms: 'path:value' (equality), "
    "'path!:value' (negated equality), or 'path>=value' (GTE -- the only "
    "comparison operator confirmed in Gateway's docs). E.g. "
    "\"category:Error,firstFoundDate>=2025-07-10\"."
)


@cli.command(name="query")
@click.option(
    "--resource",
    type=click.Choice(["comply-findings", "comply-cves", "patch-definitions"]),
    required=True,
    help="Resource to query. For Asset products/endpoints or Patch applicability -- which take a different, resource-specific flag shape -- use query-asset-products / query-asset-endpoints / query-patch-applicability instead.",
)
@click.option(
    "--cvss-version",
    type=click.Choice(sorted(_CVE_FIELDS_BY_VERSION)),
    default="v3",
    show_default=True,
    help="Only used when --resource comply-cves.",
)
@click.option("--computer-group", default=None, help="Scope to endpoints in this computer group. Used when --resource is comply-findings or comply-cves; ignored for patch-definitions.")
@click.option("--filter", "filter_str", default=None, help=_FILTER_HELP)
@_limit_option()
@_output_option()
@click.pass_context
def query(ctx, resource, cvss_version, computer_group, filter_str, limit, output_path):
    """Generic filter-based query across the resources that share this connector's `path:value` filter DSL (Comply findings/CVEs, Patch definitions). For Asset products/endpoints or Patch applicability, use the dedicated `query-asset-products` / `query-asset-endpoints` / `query-patch-applicability` commands instead -- their filters aren't expressible in this shared DSL."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        if resource == "comply-findings":
            rows = _comply_findings_search(
                client, connector, limit, computer_group=computer_group, filter_str=filter_str,
            )
        elif resource == "comply-cves":
            rows = _comply_cves_search(
                client, connector, limit, cvss_version=cvss_version,
                computer_group=computer_group, filter_str=filter_str,
            )
        else:
            rows = _patch_definitions_search(client, connector, limit, filter_str=filter_str)
    _emit_query_result(
        ctx, producer_verb=f"query-{resource}", catalog_path=[resource],
        query_desc=f"resource={resource} cvss_version={cvss_version} computer_group={computer_group!r} filter={filter_str!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="query-comply-findings")
@click.option("--computer-group", default=None, help="Scope to endpoints in this computer group (memberOf.name). Omit to scan all endpoints up to --limit.")
@click.option("--filter", "filter_str", default=None, help=_FILTER_HELP)
@_limit_option()
@_output_option()
@click.pass_context
def query_comply_findings(ctx, computer_group, filter_str, limit, output_path):
    """List Comply compliance/benchmark findings, one row per finding."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _comply_findings_search(
            client, connector, limit, computer_group=computer_group, filter_str=filter_str,
        )
    _emit_query_result(
        ctx, producer_verb="query-comply-findings", catalog_path=["comply-findings"],
        query_desc=f"computer_group={computer_group!r} filter={filter_str!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="query-comply-cves")
@click.option(
    "--cvss-version",
    type=click.Choice(sorted(_CVE_FIELDS_BY_VERSION)),
    default="v3",
    show_default=True,
    help="CVSS scoring version -- the returned field set forks per version and fields across versions are not co-selectable.",
)
@click.option("--computer-group", default=None, help="Scope to endpoints in this computer group (memberOf.name). Omit to scan all endpoints up to --limit.")
@click.option("--filter", "filter_str", default=None, help=_FILTER_HELP)
@_limit_option()
@_output_option()
@click.pass_context
def query_comply_cves(ctx, cvss_version, computer_group, filter_str, limit, output_path):
    """List Comply CVE findings, one row per finding."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _comply_cves_search(
            client, connector, limit, cvss_version=cvss_version,
            computer_group=computer_group, filter_str=filter_str,
        )
    _emit_query_result(
        ctx, producer_verb="query-comply-cves", catalog_path=["comply-cves"],
        query_desc=f"cvss_version={cvss_version} computer_group={computer_group!r} filter={filter_str!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="query-patch-definitions")
@click.option("--filter", "filter_str", default=None, help=_FILTER_HELP + " Filter shape assumed consistent with Comply's, NOT independently confirmed for this field -- see SKILL.md.")
@_limit_option()
@_output_option()
@click.pass_context
def query_patch_definitions(ctx, filter_str, limit, output_path):
    """List Patch definitions (query.patchDefinitions). Requires the 'Patch Show' permission."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _patch_definitions_search(client, connector, limit, filter_str=filter_str)
    _emit_query_result(
        ctx, producer_verb="query-patch-definitions", catalog_path=["patch-definitions"],
        query_desc=f"filter={filter_str!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="query-patch-applicability")
@click.option("--computer-group", required=True, help="Computer group to scope the sensor read to (required -- an unscoped tenant-wide sensor read is not allowed by this connector).")
@_limit_option()
@_output_option()
@click.pass_context
def query_patch_applicability(ctx, computer_group, limit, output_path):
    """List Patch applicability per endpoint via the fixed Patch sensor."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _patch_applicability_search(client, connector, limit, computer_group=computer_group)
    _emit_query_result(
        ctx, producer_verb="query-patch-applicability", catalog_path=["patch-applicability"],
        query_desc=f"computer_group={computer_group!r}",
        rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="get-patch-deployment")
@click.option("--id", "deployment_id", required=True, help="Patch deployment id.")
@click.pass_context
def get_patch_deployment(ctx, deployment_id):
    """Hydrate one Patch deployment by id (query.patchDeployment). Requires the 'Patch Deployment Read' permission."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        result = _patch_deployment_get(client, connector, deployment_id)
    click.echo(json.dumps(result, indent=2))


@cli.command(name="query-threat-response-alerts")
@_limit_option()
@_output_option()
@click.pass_context
def query_threat_response_alerts(ctx, limit, output_path):
    """List Threat Response alerts (legacy REST, best-effort -- see SKILL.md for confidence caveats)."""
    started = time.monotonic()
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        rows = _threat_response_alerts_search(client, connector, limit)
    _emit_query_result(
        ctx, producer_verb="query-threat-response-alerts", catalog_path=["threat-response-alerts"],
        query_desc="", rows=rows, output_path=output_path, started=started, connector=connector,
    )


@cli.command(name="get-threat-response-alert")
@click.option("--id", "alert_id", required=True, help="Threat Response alert GUID.")
@click.pass_context
def get_threat_response_alert(ctx, alert_id):
    """Hydrate one Threat Response alert by GUID (legacy REST, best-effort)."""
    with _mcp_client() as client:
        connector = instance.require_bound_connector(client)
        result = _threat_response_alert_get(client, connector, alert_id)
    click.echo(json.dumps(result, indent=2))


@cli.command(name="view-result")
@click.option("--ref", required=True, help="Result reference UUID from a prior query.")
@click.option("--result-set", default=0, show_default=True, type=int, help="Result-set index (from the PSV footer).")
@click.option("--row", required=True, type=int, help="Row number within the result set.")
@click.pass_context
def view_result(ctx: click.Context, ref: str, result_set: int, row: int) -> None:
    """Print the full JSON for one row from a prior query -- a pure cache read."""
    try:
        entity = cache.get_row(ref, result_set, row)
    except cache.CacheMiss as e:
        raise click.ClickException(str(e)) from e
    click.echo(json.dumps(entity, indent=2))


def _sample_rows(
    client: mcp.MCPClient,
    connector: str,
    segments: list[str],
    column: str | None,
    values: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    if len(segments) != 1 or segments[0] not in _SEARCH_FNS:
        return []
    resource = segments[0]
    rows = _SEARCH_FNS[resource](client, connector, limit)
    if column is None:
        return rows
    if not values:
        return []
    wanted = set(values)
    return [r for r in rows if str(r.get(column)) in wanted]


samplecmds.add_commands(cli, _sample_rows)


kgcommands.add_commands(
    cli,
    lambda client: _TaniumSchemaProvider(client),
    skill_name=SKILL_NAME,
    platform_label="Tanium",
    ls_path_help="UNIX-style absolute schema path. ``/`` lists Tanium connectors this skill owns; ``/<connector>/<resource>/<field>`` drills. Basename may be a glob (``*``, ``[a-c]``, ``[!a-c]*``, ``*term*``); insert ``**`` immediately before the basename for recursive search.",
    ls_docstring="List this skill's Tanium connectors' database schema by UNIX path.\n\n``--path /`` lists the connectors this skill owns; deeper paths drill into the tiers below. To search the catalog by name instead of walking it tier by tier, use the ``grep`` subcommand.\n\nHierarchy: Resource -> Field. Fields are discovered empirically from a one-row sample of each resource. ``patch-applicability`` (requires a customer-specific --computer-group) and ``patch-deployments`` (get-by-id only, no listing) are not part of this walk.",
    refresh_docstring="Walk Tanium Asset/Comply/Patch/Threat-Response schema in real time and commit catalog tiers to the KG.\n\nThe only ``tanium.py`` command that hits the connector backend for schema discovery; ``ls`` reads exclusively from the KG cache. Walks are resumable per-tier; stale schema items are pruned at the tier they disappear from.",
    grep_docstring="String-search this skill's Tanium connectors' KG catalog.\n\nMatches the pattern against each node's schema-element name, its LLM-assigned summary, and its tags. Reads the persisted knowledge graph (populated by `refresh` + the kg-learner describe pass), not the live API. Default match is a literal case-insensitive substring; use --regex for a POSIX regex or --fuzzy for typo-tolerant trigram matching. To search every connector type at once, use `kg_learner.py grep`.",
    upload_query_help="The exact query description the samples came from (must match the source search's internal query string; pass an empty string if none).",
    upload_ref_help="UUID of a recent query (read from the in-container cache).",
    upload_from_file_help="Path to a file written by one of the `query-*` commands' `--output ...`.",
    upload_catalog_path_help="POSIX-style catalog path the rows belong to (e.g. /comply-findings/<field>).",
    upload_docstring="Verify-then-upload samples from a recent query.\n\nSource is either `--ref UUID` (the cache from a recent `query-*` command) or `--from-file FILE` (a file produced by `--output`). Exactly one is required. Both enforce a read-before-write check: the agent must have run this exact query, the export cannot exceed the upload limit, and a content hash must match.\n\nOn success the rows are persisted via the hidden `upsert_data_sample` MCP tool; the server resolves --catalog-path tier-by-tier (lazily creating catalog entries) and walks each sample's nested JSON keys into child nodes under the leaf.",
)

if __name__ == "__main__":
    queryerror.run_cli(cli, PACKAGE)
