# crogl-connector-skills

> **Public mirror.** This is a public snapshot of an internal Crogl repo, published as a
> proof of concept ahead of hosting it officially under the `crogl` GitHub org. It may be
> out of sync with the latest internal state, and its history/location may change.

Customer-shareable connector packages maintained by Crogl CS.

This repo contains three artifact types:

- **Community connector types** (`community/`) -- importable connector packages with `SKILL.md`, `connector.json`, and a Python CLI under `scripts/`.
- **Standalone reference skills** (`references/`) -- markdown cookbooks for connectors that do not yet have a community connector type.
- **Provisioning guides** (`docs/provisioning/`) -- AsciiDoc-authored, PDF-built walkthroughs for setting up a connector's vendor-side credentials. The only artifact type here written for a human rather than the agent.

Community connector bundles must be self-contained. Put connector-specific recipes, quirks, rendering guidance, and guardrails in `community/<connector>/SKILL.md`, not in a separately imported reference skill. A separate reference skill for the same connector conflicts with the connector instance skill and causes redundant/corrective agent turns.

All artifacts in this repo are treated as customer-shareable. Do not include customer-specific content, live tenant URLs, secrets, Drive links, Slack references, or Crogl-internal incident names.

## Support status

| Status | Meaning |
|---|---|
| `draft` | Built and validated locally, but not proven in a live customer or demo workflow. |
| `field-tested` | Validated in acmedemo or a field/demo environment with at least one successful query, lookup, or action. |
| `customer-validated` | Confirmed working in a real customer environment or customer POC. |

## Standalone reference skills

These are for connectors that do **not** yet have a community connector type. When a community connector is created, migrate the operational guidance into the community bundle and stop importing the standalone reference independently.

| Reference skill | Paired connector type | Target product | Status | Version | Source | Distributable |
|---|---|---|---|---|---|---|
| `cb-edr-reference` | Pending community package | Carbon Black EDR (on-prem / Response), CBR 7.x | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 1.3.1 | [references/cb-edr-reference/](./references/cb-edr-reference/) | [dist/references/cb-edr-reference.zip](./dist/references/cb-edr-reference.zip) |
| `cortex-xdr-reference` | Pending community package | Cortex XDR / XSIAM, `public_api/v1` | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 1.0.2 | [references/cortex-xdr-reference/](./references/cortex-xdr-reference/) | [dist/references/cortex-xdr-reference.zip](./dist/references/cortex-xdr-reference.zip) |
| `qradar-reference` | **Superseded by `qradar` community bundle — do not import independently** | IBM QRadar SIEM, REST API v14.0 | ![superseded](https://img.shields.io/badge/status-superseded-red) | 0.1.1 | [references/qradar-reference/](./references/qradar-reference/) | — |
| `github-reference` | Pending community package | GitHub.com REST API v3 | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [references/github-reference/](./references/github-reference/) | [dist/references/github-reference.zip](./dist/references/github-reference.zip) |
| `wiz-reference` | Pending community package | Wiz CNAPP GraphQL API | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.2 | [references/wiz-reference/](./references/wiz-reference/) | [dist/references/wiz-reference.zip](./dist/references/wiz-reference.zip) |
| `jira-datacenter-reference` | **Superseded by `jira-datacenter` community bundle — do not import independently** | Jira Data Center, REST API v2 | ![superseded](https://img.shields.io/badge/status-superseded-red) | 0.1.0 | [references/jira-datacenter-reference/](./references/jira-datacenter-reference/) | [dist/references/jira-datacenter-reference.zip](./dist/references/jira-datacenter-reference.zip) |

## Community connector types

| Connector type | Target product | Status | Version | Source | Distributable |
|---|---|---|---|---|---|
| `abuseipdb` | AbuseIPDB v2 reputation API | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/abuseipdb/](./community/abuseipdb/) | [dist/community/abuseipdb.zip](./dist/community/abuseipdb.zip) |
| `bigquery` | Google BigQuery query and schema connector | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/bigquery/](./community/bigquery/) | [dist/community/bigquery.zip](./dist/community/bigquery.zip) |
| `intune` | Microsoft Intune (Graph `deviceManagement/managedDevices`) | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/intune/](./community/intune/) | [dist/community/intune.zip](./dist/community/intune.zip) |
| `purview-dlp` | Microsoft Purview DLP (Graph `security/alerts_v2`, `serviceSource eq 'dataLossPrevention'`) | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/purview-dlp/](./community/purview-dlp/) | [dist/community/purview-dlp.zip](./dist/community/purview-dlp.zip) |
| `solarwinds-service-desk` | SolarWinds Service Desk / Samanage incidents API | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/solarwinds-service-desk/](./community/solarwinds-service-desk/) | [dist/community/solarwinds-service-desk.zip](./dist/community/solarwinds-service-desk.zip) |
| `defender` | Microsoft Defender for Endpoint (`api.securitycenter.microsoft.com`) | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/defender/](./community/defender/) | [dist/community/defender.zip](./dist/community/defender.zip) |
| `windows-update-reports` | Windows Update for Business reports (Azure Monitor Log Analytics KQL query API) | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/windows-update-reports/](./community/windows-update-reports/) | [dist/community/windows-update-reports.zip](./dist/community/windows-update-reports.zip) |
| `recordedfuture` | Recorded Future read-only intelligence lookups (entities, alerts, playbook alerts, identity, links, threat actor/malware -- `api.recordedfuture.com`) | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/recordedfuture/](./community/recordedfuture/) | [dist/community/recordedfuture.zip](./dist/community/recordedfuture.zip) |
| `qradar` | IBM QRadar SIEM, REST API v14.0 (offenses, Ariel/AQL, assets, log sources; gated offense writes) | ![field-tested](https://img.shields.io/badge/status-field--tested-yellow) | 0.1.3 | [community/qradar/](./community/qradar/) | [dist/community/qradar.zip](./dist/community/qradar.zip) |
| `splunk-enterprise-security` | Splunk Enterprise Security Mission Control API (investigations, findings, notes, risk scores, id-only asset/identity lookups; read-only) | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/splunk-enterprise-security/](./community/splunk-enterprise-security/) | [dist/community/splunk-enterprise-security.zip](./dist/community/splunk-enterprise-security.zip) |
| `sophos-central` | Sophos Central / Fusion (tenant-scoped REST: alerts, cases, endpoints; read-only; SIEM events + detections deferred) | ![field-tested](https://img.shields.io/badge/status-field--tested-yellow) | 0.1.3 | [community/sophos-central/](./community/sophos-central/) | [dist/community/sophos-central.zip](./dist/community/sophos-central.zip) |
| `tanium` | Tanium Cloud -- Asset, Comply, Patch (Gateway GraphQL) and Threat Response (legacy REST, best-effort); read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/tanium/](./community/tanium/) | [dist/community/tanium.zip](./dist/community/tanium.zip) |
| `jira-datacenter` | Jira Data Center (on-prem), REST API v2 / JQL (issues, projects, fields; plus `--yes`-gated comment / create / transition / assign writes) | ![field-tested](https://img.shields.io/badge/status-field--tested-yellow) | 0.3.0 | [community/jira-datacenter/](./community/jira-datacenter/) | [dist/community/jira-datacenter.zip](./dist/community/jira-datacenter.zip) |
| `proofpoint-ices` | Proofpoint Email Security (ICES / rebranded Tessian: Defender, Guardian, Architect) -- Security Events, User Groups, User Monitoring, Anomalies, Audit Trail; read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/proofpoint-ices/](./community/proofpoint-ices/) | [dist/community/proofpoint-ices.zip](./dist/community/proofpoint-ices.zip) |
| `palo-alto-xsoar` | Palo Alto Cortex XSOAR 8 (cloud), `/xsoar/public/v1` -- incident search and get, Standard-type API key; read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/palo-alto-xsoar/](./community/palo-alto-xsoar/) | [dist/community/palo-alto-xsoar.zip](./dist/community/palo-alto-xsoar.zip) |
| `cisco-secure-access-ravpn` | Cisco Secure Access RA VPN (`api.sse.cisco.com`) -- remote-access (AnyConnect/Secure Client) historical events + active VPN sessions; read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/cisco-secure-access-ravpn/](./community/cisco-secure-access-ravpn/) | [dist/community/cisco-secure-access-ravpn.zip](./dist/community/cisco-secure-access-ravpn.zip) |
| `taegis` | Secureworks Taegis XDR (Sophos) -- single GraphQL gateway: alerts + investigations (CQL), endpoint assets; read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.2 | [community/taegis/](./community/taegis/) | [dist/community/taegis.zip](./dist/community/taegis.zip) |
| `ionic` | Ionic Data Platform -- SQL engine, indexed term search (REGEXP tokens), catalog and schema inspection | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/ionic/](./community/ionic/) | [dist/community/ionic.zip](./dist/community/ionic.zip) |
| `tines` | Tines SOAR (`/api/v1`) -- stories and actions: catalog, detail, webhook-trigger discovery with secrets redacted; read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.2.0 | [community/tines/](./community/tines/) | [dist/community/tines.zip](./dist/community/tines.zip) |
| `darktrace` | Darktrace API -- model alerts, AI Analyst incident events, and devices; read-only draft with documented HMAC signing seam | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/darktrace/](./community/darktrace/) | [dist/community/darktrace.zip](./dist/community/darktrace.zip) |
| `cortex-xdr` | Palo Alto Cortex XDR `public_api/v1` -- read-only queries across alerts, incidents, and endpoints, plus incident extra-data hydration | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/cortex-xdr/](./community/cortex-xdr/) | [dist/community/cortex-xdr.zip](./dist/community/cortex-xdr.zip) |
| `servicenow-chatops` | ServiceNow Table API, scoped to one customer-configured table -- creates a verification-request record assigned to a target user for the customer's own ServiceNow ChatOps/Now Actions flow to deliver via Slack/Teams, then polls for a reply; `--yes`-gated create/comment/close writes | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/servicenow-chatops/](./community/servicenow-chatops/) | [dist/community/servicenow-chatops.zip](./dist/community/servicenow-chatops.zip) |
| `tenable-io` | Tenable Vulnerability Management (`cloud.tenable.com`) -- `workbenches/vulnerabilities` findings query, aggregated by plugin, plus per-plugin per-asset detail; read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/tenable-io/](./community/tenable-io/) | [dist/community/tenable-io.zip](./dist/community/tenable-io.zip) |
| `runzero` | runZero Export API (read-only ET token) -- assets, services, software, vulnerabilities, wireless, sites, certificates, directory users/groups, findings, tasks, subnet utilization, SNMP ARP cache; no write capability by token design | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/runzero/](./community/runzero/) | [dist/community/runzero.zip](./dist/community/runzero.zip) |
| `cisco-xdr-incidents` | Cisco XDR Incidents & Investigations (Conure v2 API, region-specific host) -- incident search, get-by-id, per-incident/investigation detail hydration (summary, entities, observables, verdicts, events, MITRE, graph); read-only | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/cisco-xdr-incidents/](./community/cisco-xdr-incidents/) | [dist/community/cisco-xdr-incidents.zip](./dist/community/cisco-xdr-incidents.zip) |
| `pagerduty` | PagerDuty REST API v2 incident lifecycle -- create/escalate an incident (ungated, for unattended SOC-less escalation), list/get/update incidents, add a note, plus services/escalation-policies/priorities/on-calls lookups; Events API v2 (routing-key ingestion) deliberately out of scope | ![draft](https://img.shields.io/badge/status-draft-lightgrey) | 0.1.0 | [community/pagerduty/](./community/pagerduty/) | [dist/community/pagerduty.zip](./dist/community/pagerduty.zip) |

## Provisioning guides

Written for every connector type, regardless of how simple the credential looks -- see [docs/provisioning-guides.md](./docs/provisioning-guides.md) for why credential *shape* (one field vs. OAuth) doesn't predict how much real-world console work obtaining it takes. Steps that couldn't be confirmed against a live vendor console are marked best-guess/TBD in the guide itself rather than omitted.

| Provisioning guide | Connector type | Source | Built |
|---|---|---|---|
| AbuseIPDB credential provisioning | `abuseipdb` | [docs/provisioning/abuseipdb/provisioning.adoc](./docs/provisioning/abuseipdb/provisioning.adoc) | [docs/provisioning/abuseipdb/provisioning.pdf](./docs/provisioning/abuseipdb/provisioning.pdf) |
| Intune credential provisioning | `intune` | [docs/provisioning/intune/provisioning.adoc](./docs/provisioning/intune/provisioning.adoc) | [docs/provisioning/intune/provisioning.pdf](./docs/provisioning/intune/provisioning.pdf) |
| Purview DLP credential provisioning | `purview-dlp` | [docs/provisioning/purview-dlp/provisioning.adoc](./docs/provisioning/purview-dlp/provisioning.adoc) | [docs/provisioning/purview-dlp/provisioning.pdf](./docs/provisioning/purview-dlp/provisioning.pdf) |
| Microsoft Defender for Endpoint | `defender` | [docs/provisioning/defender/provisioning.adoc](./docs/provisioning/defender/provisioning.adoc) | [docs/provisioning/defender/provisioning.pdf](./docs/provisioning/defender/provisioning.pdf) |
| Windows Update for Business reports | `windows-update-reports` | [docs/provisioning/windows-update-reports/provisioning.adoc](./docs/provisioning/windows-update-reports/provisioning.adoc) | [docs/provisioning/windows-update-reports/provisioning.pdf](./docs/provisioning/windows-update-reports/provisioning.pdf) |
| Recorded Future credential provisioning | `recordedfuture` | [docs/provisioning/recordedfuture/provisioning.adoc](./docs/provisioning/recordedfuture/provisioning.adoc) | [docs/provisioning/recordedfuture/provisioning.pdf](./docs/provisioning/recordedfuture/provisioning.pdf) |
| IBM QRadar SIEM credential provisioning | `qradar` | [docs/provisioning/qradar/provisioning.adoc](./docs/provisioning/qradar/provisioning.adoc) | [docs/provisioning/qradar/provisioning.pdf](./docs/provisioning/qradar/provisioning.pdf) |
| Splunk Enterprise Security credential provisioning | `splunk-enterprise-security` | [docs/provisioning/splunk-enterprise-security/provisioning.adoc](./docs/provisioning/splunk-enterprise-security/provisioning.adoc) | [docs/provisioning/splunk-enterprise-security/provisioning.pdf](./docs/provisioning/splunk-enterprise-security/provisioning.pdf) |
| Sophos Central (Fusion) credential provisioning | `sophos-central` | [docs/provisioning/sophos-central/provisioning.adoc](./docs/provisioning/sophos-central/provisioning.adoc) | [docs/provisioning/sophos-central/provisioning.pdf](./docs/provisioning/sophos-central/provisioning.pdf) |
| Tanium credential provisioning | `tanium` | [docs/provisioning/tanium/provisioning.adoc](./docs/provisioning/tanium/provisioning.adoc) | [docs/provisioning/tanium/provisioning.pdf](./docs/provisioning/tanium/provisioning.pdf) |
| Jira Data Center credential provisioning | `jira-datacenter` | [docs/provisioning/jira-datacenter/provisioning.adoc](./docs/provisioning/jira-datacenter/provisioning.adoc) | [docs/provisioning/jira-datacenter/provisioning.pdf](./docs/provisioning/jira-datacenter/provisioning.pdf) |
| Proofpoint Email Security (ICES) credential provisioning | `proofpoint-ices` | [docs/provisioning/proofpoint-ices/provisioning.adoc](./docs/provisioning/proofpoint-ices/provisioning.adoc) | [docs/provisioning/proofpoint-ices/provisioning.pdf](./docs/provisioning/proofpoint-ices/provisioning.pdf) |
| BigQuery credential provisioning | `bigquery` | [docs/provisioning/bigquery/provisioning.adoc](./docs/provisioning/bigquery/provisioning.adoc) | [docs/provisioning/bigquery/provisioning.pdf](./docs/provisioning/bigquery/provisioning.pdf) |
| SolarWinds Service Desk credential provisioning | `solarwinds-service-desk` | [docs/provisioning/solarwinds-service-desk/provisioning.adoc](./docs/provisioning/solarwinds-service-desk/provisioning.adoc) | [docs/provisioning/solarwinds-service-desk/provisioning.pdf](./docs/provisioning/solarwinds-service-desk/provisioning.pdf) |
| Palo Alto Cortex XSOAR credential provisioning | `palo-alto-xsoar` | [docs/provisioning/palo-alto-xsoar/provisioning.adoc](./docs/provisioning/palo-alto-xsoar/provisioning.adoc) | [docs/provisioning/palo-alto-xsoar/provisioning.pdf](./docs/provisioning/palo-alto-xsoar/provisioning.pdf) |
| Cisco Secure Access RA VPN credential provisioning | `cisco-secure-access-ravpn` | [docs/provisioning/cisco-secure-access-ravpn/provisioning.adoc](./docs/provisioning/cisco-secure-access-ravpn/provisioning.adoc) | [docs/provisioning/cisco-secure-access-ravpn/provisioning.pdf](./docs/provisioning/cisco-secure-access-ravpn/provisioning.pdf) |
| Secureworks Taegis XDR credential provisioning | `taegis` | [docs/provisioning/taegis/provisioning.adoc](./docs/provisioning/taegis/provisioning.adoc) | [docs/provisioning/taegis/provisioning.pdf](./docs/provisioning/taegis/provisioning.pdf) |
| Darktrace credential provisioning | `darktrace` | [docs/provisioning/darktrace/provisioning.adoc](./docs/provisioning/darktrace/provisioning.adoc) | [docs/provisioning/darktrace/provisioning.pdf](./docs/provisioning/darktrace/provisioning.pdf) |
| Tines credential provisioning | `tines` | [docs/provisioning/tines/provisioning.adoc](./docs/provisioning/tines/provisioning.adoc) | [docs/provisioning/tines/provisioning.pdf](./docs/provisioning/tines/provisioning.pdf) |
| Palo Alto Cortex XDR credential provisioning | `cortex-xdr` | [docs/provisioning/cortex-xdr/provisioning.adoc](./docs/provisioning/cortex-xdr/provisioning.adoc) | [docs/provisioning/cortex-xdr/provisioning.pdf](./docs/provisioning/cortex-xdr/provisioning.pdf) |
| ServiceNow ChatOps credential provisioning | `servicenow-chatops` | [docs/provisioning/servicenow-chatops/provisioning.adoc](./docs/provisioning/servicenow-chatops/provisioning.adoc) | [docs/provisioning/servicenow-chatops/provisioning.pdf](./docs/provisioning/servicenow-chatops/provisioning.pdf) |
| Tenable Vulnerability Management credential provisioning | `tenable-io` | [docs/provisioning/tenable-io/provisioning.adoc](./docs/provisioning/tenable-io/provisioning.adoc) | [docs/provisioning/tenable-io/provisioning.pdf](./docs/provisioning/tenable-io/provisioning.pdf) |
| runZero credential provisioning | `runzero` | [docs/provisioning/runzero/provisioning.adoc](./docs/provisioning/runzero/provisioning.adoc) | [docs/provisioning/runzero/provisioning.pdf](./docs/provisioning/runzero/provisioning.pdf) |
| Cisco XDR Incidents & Investigations credential provisioning | `cisco-xdr-incidents` | [docs/provisioning/cisco-xdr-incidents/provisioning.adoc](./docs/provisioning/cisco-xdr-incidents/provisioning.adoc) | [docs/provisioning/cisco-xdr-incidents/provisioning.pdf](./docs/provisioning/cisco-xdr-incidents/provisioning.pdf) |
| PagerDuty credential provisioning | `pagerduty` | [docs/provisioning/pagerduty/provisioning.adoc](./docs/provisioning/pagerduty/provisioning.adoc) | [docs/provisioning/pagerduty/provisioning.pdf](./docs/provisioning/pagerduty/provisioning.pdf) |

## What each artifact is for

### Standalone reference skill

A standalone reference skill is the analyst-facing cookbook for a connector that does not yet have a community connector type. It captures:

1. Connector selection and routing hints.
2. What auth the connector injects and what the agent must never ask for.
3. Correct call shapes and vendor-specific query syntax.
4. Endpoint recipes, field values, and known API quirks.
5. Context-budget rules and rendering patterns.
6. Error handling and write-action guardrails.

Reference skills are markdown-only. They may include `references/` deep docs, but they do not include `connector.json` or Python scripts. Do not independently import a reference skill for a connector that already has a community bundle; move that content into the community bundle's `SKILL.md` instead.

### Community connector type

A community connector type is an importable Crogl connector package:

```text
community/<connector-type>/
├── SKILL.md
├── connector.json
└── scripts/
    └── <connector-type>.py
```

`connector.json` defines the display name, CLI entrypoint, credential fields, and server-side credential binding. The Python CLI uses croglng's injected `_lib` package at runtime. Do not ship `_lib` in this repo.

### Provisioning guide

A provisioning guide is a walkthrough for the human who sets up a connector's vendor-side credentials -- what account or app registration to create, what scopes/permissions to grant, whether admin consent is required, and how those values map to `connector.json`'s `credential_schema.fields`. Unlike every other artifact here, it isn't read by the agent: it's authored in AsciiDoc and built to PDF for a customer admin or FDE to use directly. See [docs/provisioning-guides.md](./docs/provisioning-guides.md).

## Installing and testing

Standalone reference skills are uploaded through the Crogl Skills UI or installed on the host filesystem under `$CROGL_HOME/var/content/skills/`. Do not upload one for a connector that has a community connector type.

Community connector types are imported with an admin-scoped Crogl CLI:

```bash
crogl import-connector-type --file dist/community/<connector-type>.zip
crogl list-connector-types
crogl create-connector --name <instance-name> --type <connector-type> --value key=value
```

Provisioning guides are not installed into Crogl at all -- they're handed directly to whoever is provisioning the connector's credentials in the vendor's console.

## Adding or changing artifacts

Use [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution rules.

High-level rules:

1. Every community connector type must be self-contained; do not ship an independently imported reference skill for the same connector.
2. Source and dist zip must be committed together.
3. Never include customer-specific content -- see CONTRIBUTING.md.
4. Community connector types must pass `crogl validate-connector` before release.
5. Status must be updated when moving from `draft` to `field-tested` or `customer-validated`.
6. Write a provisioning guide for every connector type -- credential shape (one field vs. OAuth) doesn't predict how much real console work obtaining it takes, so don't skip one just because a credential looks simple. Mark unconfirmed steps best-guess/TBD rather than omitting the guide. Commit the `.adoc` source and built `.pdf` together.

## Repo layout

```text
crogl-connector-skills/
├── references/                  # standalone markdown reference skills for connectors without community bundles
├── community/                   # importable community connector type bundles
├── dist/
│   ├── references/              # standalone reference skill upload zips
│   └── community/               # connector type import zips
├── docs/
│   ├── reference-skills.md
│   ├── community-connector-types.md
│   ├── provisioning-guides.md
│   ├── provisioning/
│   │   ├── assets/               # shared: logo, pdf-theme.yml, template.adoc, adoc2pdf.sh, README.md
│   │   └── <connector-type>/     # provisioning.adoc + built .pdf, one per connector type
├── README.md
└── CONTRIBUTING.md
```
