---
name: wiz-reference
description: "PREFER THIS SKILL for ad-hoc Wiz CNAPP GraphQL queries: 'show critical Wiz issues', 'open vulnerabilities on EC2', 'list Wiz projects', 'CVE in my environment', 'toxic-combination issues in last 7 days', 'tell me about issue I', 'find EKS clusters with public endpoint', 'CSPM configuration findings failing', 'vulnerabilities with CISA KEV exploits', 'attack-surface issues by severity'. Composes GraphQL queries and renders results as an analyst table. Wiz uses OAuth client_credentials with 24h bearer tokens; manual rotation needed until CGL-22094 lands native OAuth — see changelog. Do NOT use for Wiz write actions (resolve / reject / assign issues, create projects), workflow orchestration, multi-hop GraphSearch path queries (single-entity-class only), or DSPM (data findings, a separate surface). Wiz Cloud (commercial / commercial-US / FedRAMP) ONLY."
version: "0.1.2"
---

# Wiz — Reference / CNAPP GraphQL Query Cookbook

> **v0.1.2 (2026-06-12) — Drill-card richness.** Added `Issue.control { id name description resolutionRecommendation securitySubCategories { title category { name framework { name } } } }` to the `IssueDetail` query. `control` is a singular `Control` object that exposes compliance-framework mappings (HITRUST CSF, NIST 800-171, SWIFT CSCF, PCI DSS, ISO 27001, etc.) — the data source for "this issue maps to N compliance controls across M frameworks" rendering. Also documented: connector `base_url` config can include or exclude `/graphql`, and `path` must complement (`base_url=.../wiz.io` → `path=/graphql`, OR `base_url=.../graphql` → `path=/`). v0.1.1 documented only the first split, which fails against connectors registered with `/graphql` in the base.
>
> **v0.1.1 (2026-06-12) — Schema-correctness pass.** Live-validated against the Crogl Wiz tenant. v0.1.0 used several invented field names taken from public docs that don't match the real Wiz GraphQL schema. Corrections: root query is `issuesV2` not `issues`; runtime threats are `detections` not `detectedRuntimeThreats`; cloud assets surface via `graphSearch` and `inventoryFindings`, not a top-level `cloudResources` query; `VulnerabilityFinding` has CVE info directly on the finding (`name`, `vulnerabilityExternalId`, `score`) not nested under `vulnerability`; `vulnerableAsset` is a 13-member UNION requiring inline spreads. Severity enums differ between surfaces — Issues use `Severity` (INFORMATIONAL+); Vulnerabilities use `VulnerabilitySeverity` (NONE+). Date filters take a `DurationFilter { amount, unit }` object for relative windows, not bare strings. Real error codes observed: `UNAUTHORIZED` (scope gap), `LICENSE_MISSING` (tenant module not licensed), `GRAPHQL_VALIDATION_FAILED` (bad field name).
>
> **v0.1.0 (2026-06-12) — Initial draft, shelf-ready, NOT live-validated.** Wiz auth gap (OAuth 2.0 client_credentials with 24h bearer tokens vs Crogl's static-secret Custom Connector) documented. Tracked under [CGL-22094](https://crogl.atlassian.net/browse/CGL-22094) — OAuth 2.0 client_credentials is in the acceptance criteria, targeted Q2FY27.

## Auth gap — read this first

Wiz authenticates with **OAuth 2.0 client_credentials** at `https://auth.app.wiz.io/oauth/token` (exchange `client_id` + `client_secret` + `audience=wiz-api` for a 24h `Bearer` token). Crogl's Custom Connector injects a static `{{secret}}` into the `Authorization` header and has no token-refresh primitive on its own. Two operational modes until [CGL-22094](https://crogl.atlassian.net/browse/CGL-22094) lands:

1. **Manual-rotation interim.** Mint a fresh bearer once per day (or when 401s appear) and paste into the connector's `{{secret}}` field. The skill's 401 error-handling branch triggers this.
2. **Token-refresh sidecar.** A small local service co-located with crogld that auto-refreshes the bearer and proxies `localhost` calls to Wiz. The connector points at the sidecar; the skill is unaware.

Token-mint command (assumes `op` CLI configured for the Crogl Wiz SA):

```bash
. <(op item get "Wiz CS Service Account" --vault Employee --reveal --format json \
   | jq -r '.fields[] | select(.id=="username" or .id=="credential") | "\(.id)=\(.value)"')
curl -s -X POST 'https://auth.app.wiz.io/oauth/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_id=$username" \
  --data-urlencode "client_secret=$credential" \
  --data-urlencode "audience=wiz-api" \
  | jq -r .access_token
```

Cookbook style: no phase gates, no STOP gates, no workflow orchestration. Read the catalog, build the GraphQL document, render the result, stop. Wiz write mutations (resolve/reject Issue, change Issue note, create Project, etc.) are out of scope.

## Connector selection (run once at start)

1. Walk the available `mcp__crogld__*` MCP tools to find connector tools.
2. Pick the connector tool whose name contains any of: `wiz`, `wiz_api`, `wiz-api`, `wiz_graphql`, `wiz-graphql`, `wiz_cnapp`, `wiz-cnapp`. Match on substring — installs name their connector descriptively, and the literal MCP-tool prefix (`api_proxy_` today, may change) is implementation detail.
3. If multiple Wiz connectors are registered (e.g. one per Wiz tenant — sandbox vs production, or commercial vs FedRAMP), list them and ask which to use before issuing the call. Wiz tokens are tenant-scoped — calls authenticated against tenant A cannot read tenant B even if the GraphQL endpoint host is the same.
4. If none matches, tell the user the connector needs to be registered with `base_url=https://api.<DC>.<ENV>` (where DC is e.g. `us1`, `us2`, `us18`, `us53`, `eu1`, `eu2` per the tenant's Data Center setting, and ENV is `app.wiz.io` for commercial / `app.wiz.us` for US sovereign / `gov.wiz.io` for FedRAMP), plus an `Authorization: Bearer {{secret}}` header storing a current bearer. The skill assumes the connector's `base_url` does NOT include the `/graphql` path suffix.
5. Cache the connector tool name. Invoke it with `{ method, path, body }` — Wiz is GraphQL-only.

**Path-composition warning.** Wiz exposes exactly one GraphQL endpoint. If the connector was registered with `base_url=https://api.<DC>.<ENV>` (recommended), call with `path: "/graphql"`. If the connector was registered with `base_url=https://api.<DC>.<ENV>/graphql` (some installs), call with `path: "/"` instead — otherwise crogld composes `.../graphql/graphql` and Wiz returns 404. The first successful call against a new connector confirms which split applies; reuse that path on every subsequent call in the session.

**Tenant model: multi-tenant by Wiz subscription; one tenant per registered connector.** Never iterate across multiple Wiz connectors in a single user request unless the user explicitly asks for a cross-tenant view — that's a context-budget and authorization-blast-radius issue.

**Discovering the right region.** The user can find theirs in the Wiz portal under Profile → Tenant Info → Data Center and Regions. The skill should not guess — if the connector `base_url` is wrong, calls return DNS failure / 404 / 401-on-handshake; surface to the user, do not retry. The `dc` claim on a decoded bearer also names the data center (e.g. `dc: us53` → `api.us53.app.wiz.io`).

## Auth recap

- Single header: `Authorization: Bearer <token>` (injected by the connector — never construct or prompt for the token).
- Bearer TTL is **24 hours**. The token-mint exchange returns `expires_in: 86400` (seconds). At ~24h the connector starts returning 401 on every call.
- 401 → bearer is expired or invalid. **Stop, surface, and tell the user to rotate the connector secret** (or restart the token-refresh sidecar). Do NOT retry — the connector has no refresh primitive on its own.
- TLS: Wiz endpoints use standard publicly-issued TLS certs. `skip_tls_verification` should be **off** on the connector. If a customer's egress proxy MITMs TLS, they need to add the proxy CA to crogld's trust store (see Crogl Setup Guide §5c for the RHEL trust-anchor procedure) — never paper over with `skip_tls_verification: true`.

## Tool call shape (critical)

Wiz is **GraphQL-only**. Every call is `POST /graphql` with a JSON body containing a `query` (or `mutation` — not used in this skill) document and a `variables` map.

The connector tool splits URL into separate fields (method, path, query, body, headers). **The full request URL is always `<base_url>/graphql`** — there are no other URL paths on the Wiz API. Depending on connector config, `path` is either `"/graphql"` (when `base_url` is just the API host) or `"/"` (when `base_url` already ends in `/graphql`). See the path-composition warning in the Connector selection section. **No query-string params** — Wiz ignores them. All inputs go in the JSON body.

```json
{
  "method": "POST",
  "path": "/graphql",
  "body": "{\"query\":\"query IssuesTable($filterBy: IssueFilters, $first: Int, $after: String) { issuesV2(filterBy: $filterBy, first: $first, after: $after) { nodes { id severity status type createdAt } pageInfo { hasNextPage endCursor } } }\",\"variables\":{\"first\":25,\"filterBy\":{\"severity\":[\"CRITICAL\"],\"status\":[\"OPEN\"]}}}",
  "headers": {"Content-Type": "application/json"}
}
```

The `body` field is a JSON-encoded string, not a nested object — the connector forwards it verbatim. Always set `Content-Type: application/json`.

**Common mistakes to avoid:**

- **Embedding the query in the URL as `?query=…`** — Wiz expects the document in the body, not the query string.
- **Sending the GraphQL document as a nested object in `body`** — `body` is a JSON-encoded string; the connector forwards it as-is. If you pass `body: {"query": "..."}` (unencoded) the connector serializes incorrectly.
- **Forgetting `Content-Type: application/json`** — Wiz returns 415 Unsupported Media Type without it.
- **Inlining enum values as JSON strings in the GraphQL document literal** — GraphQL enums in inline document literals are **unquoted identifiers** (`severity: [HIGH, CRITICAL]`), but in `variables` they're **JSON strings** (`"severity": ["HIGH", "CRITICAL"]`). Wiz rejects inline-quoted enums with `Severity cannot represent value: "HIGH"` and code `GRAPHQL_VALIDATION_FAILED`. The recipes below all use variables — never hand-inline enum values into the document body.

## Context budget rules

1. **Default `first: 25`** on every list query (`issuesV2`, `vulnerabilityFindings`, `inventoryFindings`, `configurationFindings`, etc.). Wiz allows much higher (typically up to 500 on top-level connections) but enterprise tenants return 100s of KB per page — start small.
2. **Hard cap: `first: 100`** even if the user asks for "all" / "everything" / "no row limit." Beyond that, narrow the filter or paginate explicitly.
3. **Don't auto-paginate.** The `pageInfo.hasNextPage` + `endCursor` is for explicit "give me the next page" follow-ups. Wiz caps total paginated results at **10,000** regardless — past that, the query needs to be more selective.
4. **30 KB auto-spill rule.** If a tool result exceeds 30 KB, Claude Code auto-spills to a file at `/home/crogl/.claude/projects/.../tool-results/`. The agent reads specific fields via `jq`, doesn't `cat` the whole file. Wiz `entitySnapshot` and `vulnerableAsset` objects can be large — request only the fields you'll render.
5. **Summarize and discard.** After rendering the table, drop the raw JSON from your reasoning. Issue / vulnerability details are unbounded — never carry the full response forward across follow-up calls.
6. **Field whitelist over `*`.** GraphQL lets you ask for only the fields you need. Always do — `entitySnapshot { id name type subscriptionExternalId cloudPlatform region }` is ~120 bytes per row; pulling the full snapshot (24 fields) is several KB per row.
7. **Rate limit awareness.** Wiz enforces **10 calls/sec per service account** and **100 calls/sec per tenant**, with a **5-minute request timeout** per call. If a list query takes more than a few seconds, narrow the filter rather than waiting.

## Query catalog (Wiz GraphQL surfaces relevant to v0.1.1)

| Top-level query | Use | Notes |
|-----------------|-----|-------|
| `issuesV2(first, after, filterBy: IssueFilters, filterScope, orderBy)` | Wiz Issues — the canonical "what's broken" surface (CSPM + toxic combinations + threat detection + attack surface). Most analyst queries land here. | Returns `IssueConnection`. Each `nodes[]` element is an `Issue` (50 fields). |
| `issue(id: ID!)` | Drill on one Issue by UUID. | Returns `Issue`. |
| `vulnerabilityFindings(first, after, filterBy: VulnerabilityFindingFilters, orderBy)` | CVE-level findings on cloud workloads. | Filter is very large (135 fields). CVE info is on the finding directly (`name`, `vulnerabilityExternalId`, `score`), not nested. |
| `vulnerabilityFinding(id: ID!)` | Drill on one CVE finding. | |
| `configurationFindings(first, after, filterBy: ConfigurationFindingFilters, orderBy, quick)` | CSPM rule pass/fail (CIS, NIST, custom, etc.). | |
| `inventoryFindings(first, after, filterBy: InventoryFindingFilters, orderBy)` | Inventory hygiene findings — assets missing tags / lifecycle issues / etc. | NOT a generic cloud-resource list. For "find me all S3 buckets" use `graphSearch`. |
| `projects(first, after, filterBy: ProjectFilters, orderBy)` | Project (scope) listing. | Always run first if the user names a project by display name; you need the UUID to scope subsequent calls. |
| `graphSearch(query, first, after)` | Entity-class queries (Wiz's signature feature). | Single-entity-class only in v0.1.1 — multi-hop relationship traversal is out of scope. |
| `threatCenterItems(first, after, filterBy, orderBy)` | Wiz Threat Center — curated threat-intel items (advisories, campaigns). | Pure threat intel; no Defend license required. |
| `threatCenterActors(first, after, filterBy, orderBy)` | Threat actors tracked in Threat Center. | |
| `detections(first, after, filterBy, orderBy, …)` | Runtime detections (Wiz Defend). | **Requires `CLOUD_EVENTS_AND_DETECTION_DEFEND` or `RUNTIME_SENSOR` license.** Without it, returns `LICENSE_MISSING`. |
| `cloudEvents(first, after, filterBy, …)` | Cloud control-plane events (Wiz Defend). | Same license dependency as `detections`. |

### Surfaces NOT covered in v0.1.1

| Surface | Why excluded |
|---------|--------------|
| Any `update*`, `create*`, `delete*` mutation | Write actions — needs a dedicated workflow skill with approval gating. |
| Complex `graphSearch` with multi-hop relationship traversal | DSL is non-trivial; v0.1.1 is single-entity-class only. Anything beyond → needs its own skill. |
| `dataFindings`, `dataFindingsV2`, DSPM surfaces | Separate Wiz module with its own filter shape. |
| `accessFindings`, `excessiveAccessFindings`, `iacFindings`, `sastFindings`, `secretDetectionRules`, `malwareFindings`, `aiSecurityFindings`, `softwareSupplyChainFindings`, `penetrationTestFindings`, `cloudCostMonitorFindings` | Each is its own specialized module; cover individually only when a customer use case appears. |
| Identity admin (`users`, `serviceAccounts`, `samlIdps`, `apiKeys`) | Admin surface, blast radius. |

If the user asks for any of the above, say so and stop — don't improvise.

## Field values (use ONLY these — do not guess; introspected from the live schema)

### Severity enums (these are DIFFERENT — pay attention)

- **Issue `Severity`** (use in `IssueFilters.severity`): `INFORMATIONAL`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`
- **`VulnerabilitySeverity`** (use in `VulnerabilityFindingFilters.severity`, `vendorSeverity`, etc.): `NONE`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` — note `NONE` not `INFORMATIONAL`

Translating "critical" / "high" from a user's natural-language request to the right enum depends on which surface you're querying. If unsure, ask.

### Status enums

- **`IssueStatus`** (use in `IssueFilters.status`): `OPEN`, `IN_PROGRESS`, `RESOLVED`, `REJECTED`
- **`FindingCommonStatus`** (use in `VulnerabilityFindingFilters.status`, `ConfigurationFindingFilters.status`, etc.): `OPEN`, `IN_PROGRESS`, `RESOLVED`, `REJECTED` (same values, different enum name)

### Issue type

- **`IssueType`** (use in `IssueFilters.type`): `TOXIC_COMBINATION`, `THREAT_DETECTION`, `CLOUD_CONFIGURATION`, `ATTACK_SURFACE` (4 values, no others)

### Cloud platform / asset type

- **`CloudPlatform`** (string enum, common values): `AWS`, `Azure`, `GCP`, `OCI`, `Alibaba`, `Kubernetes`, `EKS`, `AKS`, `GKE`, `OpenShift`, `GitHub`, `GitLab`, `Bitbucket`, `Terraform`, `Snowflake`, `Databricks`, `Okta`, `MongoDBAtlas`, `Microsoft365`, `Slack`, `Anthropic`, `OpenAI`, `Salesforce`, `ServiceNow`, `SelfHosted`, `Unknown`. (Full enum has 50+ values — these are the common ones.)
- **`VulnerableAssetObjectType`** (filter by asset type on vuln findings): `VIRTUAL_MACHINE`, `SERVERLESS`, `CONTAINER_IMAGE`, `CONTAINER`, `REPOSITORY`, `REPOSITORY_BRANCH`, `IDE`, `ENDPOINT`, `DATA_WORKLOAD`, `WEB_SERVICE`, `COMPUTE_INSTANCE_GROUP`, `DATABASE`, `DB_SERVER`, `MAP_REDUCE_CLUSTER`, `KUBERNETES_CLUSTER`, `BUCKET`, `NETWORK_ADDRESS`, `WORKSTATION`, `FIREWALL`, `NETWORK_APPLIANCE`, `MESSAGING_SERVICE`, `VIRTUAL_MACHINE_IMAGE`, `DATA_WORKFLOW`.

### Cloud-account identifier conventions

- `subscriptionExternalId` (String) — the **cloud-provider native ID**: AWS account number, Azure subscription GUID, GCP project ID. Use this when the user names a cloud account naturally.
- `subscriptionId` (String) — Wiz-internal UUID for the cloud account. Different from external ID. Only use when explicitly given.

### Date filter shape

Both `IssueDateFilter` and `VulnerabilityDateFilters` are objects with these fields (pick one form per filter):

| Field | Type | Use |
|-------|------|-----|
| `before` | `DateTime` (ISO 8601 with `Z`) | Absolute upper bound |
| `after` | `DateTime` (ISO 8601 with `Z`) | Absolute lower bound |
| `inLast` | `DurationFilter { amount: Int, unit: <Unit> }` | Relative window into the past |
| `beforeLast` | `DurationFilter` | More than N units ago |
| `inNext` | `DurationFilter` | Relative window into the future (for due-date filters) |
| `afterNext` | `DurationFilter` | More than N units in the future |

**`DurationFilterValueUnit` values:** `DurationFilterValueUnitMinutes`, `DurationFilterValueUnitHours`, `DurationFilterValueUnitDays`, `DurationFilterValueUnitWeeks`, `DurationFilterValueUnitMonths`. (The `DurationFilterValueUnitInvalid` value exists but never use it.) Note the **`DurationFilterValueUnit` prefix on every value** — Wiz's enum names are unusually verbose.

### Common string filter shape

Several filters on `VulnerabilityFindingFilters` (and elsewhere) use `CommonStringFilter`:

```json
{ "equals": ["abc"], "contains": ["x","y"], "startsWith": ["pre-"], "matchesRegex": ["^foo.*"], "isSet": true }
```

Available ops: `equals` / `notEquals` / `startsWith` / `doesNotStartWith` / `endsWith` / `doesNotEndWith` / `contains` / `doesNotContain` / `matchesRegex` / `containsAll` / `hasAnyPart` / `doesNotHaveAnyPart` / `isSet`. All array-valued except `isSet` (Boolean).

### Common number filter shape

```json
{ "equals": 8.5, "greaterThan": 7.0, "lessThan": 10.0, "isSet": true }
```

Available ops: `equals` / `notEquals` / `greaterThan` / `lessThan` / `isSet`. Used for CVSS scores, etc.

### Fields needing user input

- `projectId` — UUID, fetch via `projects` query. Don't guess.
- `subscriptionId` — Wiz-internal cloud-account UUID. Use `subscriptionExternalId` (the AWS account number / Azure sub GUID) when the user is talking about their cloud account naturally; only use `subscriptionId` when explicitly given.
- `issueId`, `vulnerabilityFindingId` — UUIDs only; if the user says "the public S3 bucket issue" without a UUID, run a filtered list query first, then drill on the returned ID.

## GraphQL query documents

### `IssuesTable`

```graphql
query IssuesTable($filterBy: IssueFilters, $first: Int = 25, $after: String, $orderBy: IssueOrder) {
  issuesV2(filterBy: $filterBy, first: $first, after: $after, orderBy: $orderBy) {
    nodes {
      id
      severity
      status
      type
      createdAt
      resolvedAt
      description
      sourceRules { id name }
      entitySnapshot {
        id name type nativeType
        subscriptionExternalId cloudPlatform region
        cloudProviderURL
      }
      projects { id name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

### `IssueDetail`

Prefer `control { ... }` (singular `Control` with rich compliance-framework mappings) over `sourceRules { name }` (list with names only) for drill rendering — `control.securitySubCategories[].category.framework.name` is what powers the "this issue maps to N compliance controls across M frameworks" content. Both point at the same underlying rule entity; keep `sourceRules` only if you specifically want list-shape for templating.

```graphql
query IssueDetail($id: ID!) {
  issue(id: $id) {
    id
    severity status type
    createdAt resolvedAt statusChangedAt dueAt updatedAt
    description resolutionNote
    control {
      id name description resolutionRecommendation
      securitySubCategories {
        title
        category { name framework { name } }
      }
    }
    entitySnapshot {
      id name type nativeType
      subscriptionId subscriptionExternalId subscriptionName
      cloudPlatform region resourceGroupExternalId
      cloudProviderURL externalId providerId
      kubernetesClusterName kubernetesNamespaceName
    }
    projects { id name slug }
    notes { id text createdAt }
    serviceTickets { id externalId url }
    assignee { id name }
    url
    validatedAsExploitable
    hasCodeRemediation
  }
}
```

### `VulnerabilityFindingsTable`

`VulnerableAsset` is a **UNION** with 13 concrete member types. The fields below (`id`, `name`, `type`, `region`, `cloudPlatform`, `subscriptionExternalId`) are on every member — the inline-spread pattern below works without a specific type discriminator. If you need members-specific fields (e.g. `imageTag` on `VulnerableAssetContainerImage`), add a `... on <Type> { ... }` spread per concrete type.

```graphql
query VulnerabilityFindingsTable($filterBy: VulnerabilityFindingFilters, $first: Int = 25, $after: String, $orderBy: VulnerabilityFindingOrder) {
  vulnerabilityFindings(filterBy: $filterBy, first: $first, after: $after, orderBy: $orderBy) {
    nodes {
      id
      name
      vulnerabilityExternalId
      severity
      status
      score
      hasExploit
      hasCisaKevExploit
      hasFix
      cisaKevReleaseDate
      firstDetectedAt
      lastDetectedAt
      vulnerableAsset {
        ... on VulnerableAssetVirtualMachine { id name type region cloudPlatform subscriptionExternalId }
        ... on VulnerableAssetContainerImage  { id name type region cloudPlatform subscriptionExternalId }
        ... on VulnerableAssetContainer       { id name type region cloudPlatform subscriptionExternalId }
        ... on VulnerableAssetServerless      { id name type region cloudPlatform subscriptionExternalId }
        ... on VulnerableAssetCommon          { id name type region cloudPlatform subscriptionExternalId }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

### `VulnerabilityFindingDetail`

```graphql
query VulnerabilityFindingDetail($id: ID!) {
  vulnerabilityFinding(id: $id) {
    id name vulnerabilityExternalId
    severity status
    score cnaScore nvdScore epssProbability epssPercentile
    hasExploit hasCisaKevExploit hasFix
    cisaKevReleaseDate cisaKevDueDate
    description remediation
    detailedName version fixedVersion recommendedVersion
    firstDetectedAt lastDetectedAt resolvedAt
    detectionMethod
    validatedInRuntime runtimeValidationResult
    isMaliciousPackage
    vulnerableAsset {
      ... on VulnerableAssetVirtualMachine { id name type region cloudPlatform subscriptionExternalId subscriptionName }
      ... on VulnerableAssetContainerImage  { id name type region cloudPlatform subscriptionExternalId subscriptionName }
      ... on VulnerableAssetCommon          { id name type region cloudPlatform subscriptionExternalId subscriptionName }
    }
    projects { id name }
  }
}
```

### `ConfigurationFindingsTable`

Note: on `ConfigurationFindingResource`, cloud-account info lives under `subscription { externalId name cloudProvider }` (a nested `CloudAccount`), NOT a top-level `subscriptionExternalId` like `IssueEntitySnapshot` and `VulnerableAsset*` types. Wiz schemas are inconsistent on this — query against the type, not by analogy. Severity here is its own enum `ConfigurationFindingSeverity` (string values match `Severity`: INFORMATIONAL / LOW / MEDIUM / HIGH / CRITICAL).

```graphql
query ConfigurationFindingsTable($filterBy: ConfigurationFindingFilters, $first: Int = 25, $after: String, $orderBy: ConfigurationFindingOrder) {
  configurationFindings(filterBy: $filterBy, first: $first, after: $after, orderBy: $orderBy) {
    nodes {
      id
      result
      severity
      rule { id name }
      resource {
        id name type cloudPlatform region
        subscription { externalId name cloudProvider }
      }
      analyzedAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

### `ProjectsList`

```graphql
query ProjectsList($first: Int = 50, $filterBy: ProjectFilters) {
  projects(first: $first, filterBy: $filterBy) {
    nodes { id name slug description }
  }
}
```

### `GraphSearchSingleEntity` (single-entity-class only)

```graphql
query GraphSearchSingleEntity($query: GraphEntityQueryInput!, $first: Int = 25) {
  graphSearch(query: $query, first: $first) {
    nodes {
      entities { id name type properties }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

## Query recipes

| User intent | Document | Variables |
|-------------|----------|-----------|
| "Show critical open issues" | `IssuesTable` | `{"first":25,"filterBy":{"severity":["CRITICAL"],"status":["OPEN"]}}` |
| "High/critical issues in last 7 days" | `IssuesTable` | `{"first":25,"filterBy":{"severity":["HIGH","CRITICAL"],"status":["OPEN"],"createdAt":{"inLast":{"amount":7,"unit":"DurationFilterValueUnitDays"}}}}` |
| "Toxic-combination issues only" | `IssuesTable` | `{"first":25,"filterBy":{"type":["TOXIC_COMBINATION"],"status":["OPEN"]}}` |
| "Issues in project P" | `IssuesTable` | `{"first":25,"filterBy":{"project":["<projectId>"]}}` (run `ProjectsList` first if user named the project by display name) |
| "Issues on AWS account A" | `IssuesTable` | `{"first":25,"filterBy":{"cloudAccountOrCloudOrganizationId":["<awsAccountNumber>"]}}` (uses external ID, not Wiz UUID) |
| "Tell me about issue I" (drill) | `IssueDetail` | `{"id":"<issueUUID>"}` |
| "Critical vulnerabilities" | `VulnerabilityFindingsTable` | `{"first":25,"filterBy":{"severity":["CRITICAL"],"status":["OPEN"]}}` (note: `VulnerabilitySeverity` enum, so `CRITICAL` not `INFORMATIONAL`) |
| "Vulnerabilities with CISA KEV exploits" | `VulnerabilityFindingsTable` | `{"first":25,"filterBy":{"hasCisaKevExploit":true,"status":["OPEN"]}}` |
| "CVE-X in my environment" | `VulnerabilityFindingsTable` | `{"first":25,"filterBy":{"vulnerabilityExternalIdV2":{"equals":["<CVE-id>"]}}}` |
| "Vulns on EC2 instances" | `VulnerabilityFindingsTable` | `{"first":25,"filterBy":{"assetType":["VIRTUAL_MACHINE"],"cloudPlatforms":["AWS"]}}` |
| "Vulns on AWS account A" | `VulnerabilityFindingsTable` | `{"first":25,"filterBy":{"subscriptionExternalId":["<awsAccountNumber>"]}}` |
| "Tell me about CVE finding F" (drill) | `VulnerabilityFindingDetail` | `{"id":"<findingUUID>"}` |
| "CIS controls failing" | `ConfigurationFindingsTable` | `{"first":25,"filterBy":{"result":["FAIL"]}}` (refine framework via `rule` filter once introspected) |
| "List Wiz projects" | `ProjectsList` | `{"first":50}` |
| "Get next page of <prior result>" | re-issue prior query | append `"after":"<endCursor-from-previous-response>"` |

**Accepted user date phrasings — convert before composing the call:**

| User says | Convert to |
|-----------|------------|
| "last 24 hours" / "last day" | `"inLast":{"amount":24,"unit":"DurationFilterValueUnitHours"}` |
| "last 7 days" / "last week" | `"inLast":{"amount":7,"unit":"DurationFilterValueUnitDays"}` |
| "last month" | `"inLast":{"amount":1,"unit":"DurationFilterValueUnitMonths"}` |
| "yesterday" | `{"after":"<yesterday 00:00:00>Z","before":"<today 00:00:00>Z"}` (use absolute form for single-day windows) |
| "in May" / "May 2026" | `{"after":"2026-05-01T00:00:00Z","before":"2026-06-01T00:00:00Z"}` |
| "since the breach last week" / vague | Ask the user for a date or a relative window; don't guess. |
| `5/12/2026` (ambiguous US vs EU) | Ask the user to disambiguate before composing. |

Wiz uses ISO 8601 WITH the `Z` suffix for absolute timestamps. For relative windows, prefer the `DurationFilter` shape — it's the idiomatic Wiz form and the server computes the cutoff relative to its own clock (no client-clock drift).

## GraphSearch — single-entity-class recipes only

Wiz's `graphSearch` query is the signature feature: it lets you express questions like "find Kubernetes clusters with public ingress" by naming an entity type and predicates over its properties. The DSL is a JSON object describing entity types and (optionally) relationships, with `type`, `select`, `where`, and `relationships` clauses.

**v0.1.1 scope:** single-entity-class queries only. Anything involving a path traversal (two or more entity classes connected by `relationships:`) is **out of scope** — those queries need a dedicated skill that can render the relationship graph appropriately.

### Recipe

Variables example for "show me EKS clusters with public endpoint":

```json
{
  "first": 25,
  "query": {
    "type": ["KUBERNETES_CLUSTER"],
    "select": true,
    "where": {
      "hasPublicEndpoint": {"EQUALS": true}
    }
  }
}
```

If the user's question involves multiple entity classes ("S3 buckets that an EC2 instance with public IP can access"), surface the limitation: "this needs a path-relationship query — out of scope for the reference skill, route to a dedicated graph workflow." Don't improvise multi-hop documents — they trip query-complexity limits and return 400 with cryptic error messages.

## Rendering pattern

Scannable tables, default columns:

- **Issues list:** `id` (last 8 chars) · `severity` · `status` · `type` · `entitySnapshot.name` · `entitySnapshot.type` · `entitySnapshot.cloudPlatform` · `entitySnapshot.subscriptionExternalId` · `createdAt` (date only)
- **Vulnerability findings:** `vulnerabilityExternalId` (CVE) · `severity` · `score` · `hasCisaKevExploit` (✓/-) · `hasExploit` (✓/-) · `hasFix` (✓/-) · `vulnerableAsset.name` · `vulnerableAsset.type` · `subscriptionExternalId` · `lastDetectedAt` (date only)
- **Configuration findings:** `result` · `severity` · `rule.name` · `resource.name` · `resource.cloudPlatform` · `resource.subscription.externalId` (display as "AWS account / Azure sub") · `analyzedAt` (date only)
- **Projects:** `id` (last 8 chars) · `name` · `slug` · `description` (truncate 60)

≤25 rows → inline. >25 → show 25 + "(Showing 25 of N+ rows; ask to narrow or page)" using `pageInfo.hasNextPage` as the signal. Render once, then drop the raw response.

### Drill-render card

When the user says "tell me about issue I" / "describe issue I" / "what's the deal with issue I", run `IssueDetail` and surface as a card:

- **Issue ID** — `id` (full UUID; render in monospace for copy-paste)
- **Severity / Status / Type** — three top-line fields
- **Rule** — `control.name` (or fall back to `sourceRules[0].name` if `control` is null), plus the description from `control.description` (truncate 400 chars)
- **Entity** — `entitySnapshot.name` (`entitySnapshot.nativeType` or `entitySnapshot.type`) in subscription `entitySnapshot.subscriptionExternalId` (`entitySnapshot.subscriptionName`) / region `entitySnapshot.region` (`entitySnapshot.cloudPlatform`); include `entitySnapshot.cloudProviderURL` as a clickable link if present
- **Timeline** — `createdAt` → `resolvedAt` (or "still open" if null); `dueAt` if set; `statusChangedAt` if it differs from `createdAt`
- **Remediation** — `control.resolutionRecommendation` rendered as markdown (Wiz returns markdown-formatted strings — surface as-is, do not truncate aggressively; this is the highest-value content of the card)
- **Compliance mappings** — `control.securitySubCategories[]` grouped by `category.framework.name`. Render as: "Maps to N controls across M frameworks: HITRUST CSF v11.2 (3), NIST 800-171 Rev 3 (2), SWIFT CSCF v2025 (4), …". If the user asks "which frameworks", expand the grouping into the full per-framework list of subCategory titles
- **Analyst note** — `resolutionNote` if set, otherwise count `notes[]` with "N notes — ask to see them"
- **Service ticket** — `serviceTickets[0].url` if present
- **Assignee** — `assignee.name` if present
- **Wiz portal URL** — `url` (format: `https://app.<DC>.app.wiz.io/issues#~(issue~'<id>)`)

Same shape for `VulnerabilityFindingDetail`: lead with CVE name + score, then asset, then `description` + `remediation` (both truncated to ~400 chars), then exploit-status flags (`hasExploit`, `hasCisaKevExploit`, `cisaKevDueDate`, `epssProbability`).

## Error handling

GraphQL is HTTP 200 by default for application-level errors. **`status: 200` does NOT mean the query succeeded.** Always inspect the response body:

```json
{ "data": null, "errors": [ {"message": "...", "extensions": {"code": "..."}} ] }
```

If `errors` is non-empty, the query failed (or partially failed — Wiz returns `data` populated where it could). Surface the named errors.

| HTTP | Body signal | Cause | Action |
|------|-------------|-------|--------|
| 200 | `errors[].extensions.code == "GRAPHQL_VALIDATION_FAILED"` | Field doesn't exist on this schema version, or wrong type entirely (e.g. `severity: 8` instead of `severity: "HIGH"`, or pluralized/snake-case variant) | Surface verbatim; the message names the offending field with line:column. Fix the query — don't loop. |
| 200 | `errors[].extensions.code == "UNAUTHORIZED"` with `extensions.effectiveScopes` and `extensions.requiredScopes` | SA scope gap — connector's bearer is for an SA that lacks the read scope for this query | Stop, name the missing scope from `requiredScopes` (e.g. `read:projects`, `read:issues`). The Wiz admin has to update the SA. |
| 200 | `errors[].extensions.code == "LICENSE_MISSING"` with `extensions.requiredFeatures` | Tenant doesn't have the Wiz product/module required (e.g. `detections` needs `CLOUD_EVENTS_AND_DETECTION_DEFEND`) | Stop, name the missing feature from `requiredFeatures`. This is a license / commercial issue, not a scope or query issue — surface and don't retry. |
| 200 | `errors[].extensions.code == "BAD_USER_INPUT"` | Invalid filter shape, wrong enum value (`severity:["INFO"]` instead of `INFORMATIONAL`), or unparseable date | Surface verbatim; the message names the field. Re-check against the "Field values" section above. |
| 200 | `errors[].extensions.code == "QUERY_COMPLEXITY_LIMIT_EXCEEDED"` | Query is too deep / too many fields / too high `first` | Narrow the query: drop nested fields, lower `first`, split into multiple calls. |
| 200 or 429 | `errors[].message contains "rate limit"` | Tenant rate limit (10/sec SA, 100/sec tenant) | Stop, surface, tell the user to wait — never retry in a tight loop. |
| 400 | Plaintext, often `Could not parse JSON` | Body is malformed JSON or wrong `Content-Type` | Re-encode `body` as a valid JSON string with `Content-Type: application/json`. |
| 401 | `Unauthorized` | Bearer expired or invalid | **Stop and surface — the connector secret needs to be rotated.** This is the common case at the 24h boundary. Do NOT retry. |
| 404 | `Not Found` on `/graphql` | Connector `base_url` is wrong (wrong region or wrong environment — commercial vs FedRAMP) | Stop, ask the user to verify the Wiz Data Center setting under Profile → Tenant Info. |
| 5xx | Various | Wiz outage | Stop, surface — these are transient. Don't auto-retry. |
| TLS handshake | "x509: certificate signed by unknown authority" or "EOF" | Customer egress proxy MITMs TLS without trusted CA, OR (rare) Wiz cert chain mismatch | Stop, escalate — never set `skip_tls_verification: true`. See Crogl Setup Guide §5c. |

### 24-hour token expiry signal

When `errors[].message` is `Unauthorized` / HTTP 401 and **every recent call against this connector also returned 401**, the bearer is almost certainly past its 24h TTL — surface:

> "The Wiz bearer token in this connector has expired (24h TTL). Rotate the connector's `{{secret}}` with a fresh `access_token` from `auth.app.wiz.io/oauth/token` (POST `grant_type=client_credentials&client_id=<id>&client_secret=<secret>&audience=wiz-api`) and retry. Long-term, CGL-22094 will land OAuth client_credentials support in the Custom Connector."

Don't retry; this is a process gap that requires human (or sidecar) action.

## Guardrails

- **Read-only for v0.1.x.** No mutations — no `updateIssue`, no `resolveIssue`, no `createProject`. If the user asks to "close this issue in Wiz" or "mark as resolved", say so and stop. A future workflow skill will add the mutation surface with approval gating.
- **No automated verdicts** (CONFIRMED / LIKELY / NOT-A-CAMPAIGN). Render the data; let the analyst judge.
- **No write-back to investigations** / `create_investigation` tool.
- **Tenant scoping rules.** If multiple Wiz connectors are registered, ask which to use; never sweep across all of them silently.
- **Respect the user's scope.** "Critical issues" means severity `CRITICAL` only, not "top 25 sorted by severity." "Last 24 hours" means a time filter, not "newest 25."
- **No GraphSearch path traversal.** Single-entity-class only in v0.1.x. Multi-hop relationship queries route to a future dedicated skill.
- **Don't fabricate field values.** Enum values listed under "Field values" are the only ones to use; the live schema is what's authoritative, not vendor docs.
- **Render before pivoting.** Answer the question asked; don't pre-emptively chain into vulnerability findings just because the issue type is `THREAT_DETECTION`.
- **Never paste the bearer back to the user, even in error messages.** If the bearer is expired, say "rotate the connector secret" — don't echo `Authorization: Bearer <token>` back, even partially.
