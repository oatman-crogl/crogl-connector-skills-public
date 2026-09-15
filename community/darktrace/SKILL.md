---
name: darktrace
description: Drive a draft Darktrace connector via the `darktrace.py` CLI. Query model alerts, AI Analyst incident events, and tracked devices using the documented Darktrace API surfaces; uses Crogl's server-side `darktrace_hmac` provider.
version: "0.1.0"
status: draft
---

# Darktrace

![status](https://img.shields.io/badge/status-draft-lightgrey) ![version](https://img.shields.io/badge/version-0.1.0-blue)

> **v0.1.0** — 2026-09-15 — Version and changelog tracking added retroactively; see below for this bundle's actual build/validation history.

Connector manifest: [connector.json](./connector.json).

CLI: [scripts/darktrace.py](./scripts/darktrace.py). The standalone signing helper is [scripts/darktrace_signing.py](./scripts/darktrace_signing.py).

This connector is **draft** because no Darktrace test environment is available. The endpoint and authentication guidance below is based only on the supplied Darktrace documentation and the merged Crogl `darktrace_hmac` provider implementation. Do not treat unrelated public documentation as authoritative for this bundle.

## Current integration boundary

Crogl now provides the `darktrace_hmac` server-side auth provider. The connector manifest maps the public/private token fields to that provider; Crogl signs each outgoing request after the final path, query, and body are built. The bundled CLI continues to use the normal `proxy.dispatch` path and never receives plaintext credentials.

The provider supports GET requests and the current Crogl implementation also handles documented POST/DELETE body-signing forms. The bundle exposes read-only commands only. The standalone [signing helper](./scripts/darktrace_signing.py) remains a documentation-grounded reference and unit-test aid; it is not used to bypass Crogl auth.

## Authentication

The connector setup fields are:

- `host` -- Darktrace appliance hostname or IP without scheme or path.
- `public_token` -- public token from a Darktrace API token pair.
- `private_token` -- private token from the same pair; Darktrace displays it only when the pair is created.
- `skip_tls_verification` -- optional off-by-default setting for an appliance with a self-signed or otherwise untrusted certificate.

Darktrace requires these headers on every request:

- `DTAPI-Token: <public token>`
- `DTAPI-Date: <UTC date>`
- `DTAPI-Signature: <HMAC-SHA1 digest>`

The signature is calculated as:

```text
HMAC-SHA1(private_token, request_path_and_query + "\n" + public_token + "\n" + date)
```

Sign only the endpoint path and query; do not include the hostname. Use the exact same date value in the signature and `DTAPI-Date` header. Darktrace requires the date to be within 30 minutes of the instance system time, in UTC. The compact `YYYYMMDDTHHMMSS` form is used by the helper.

The supplied documentation describes per-user and global token pairs. Per-user tokens are restricted by the associated local Threat Visualizer user's permissions. The documentation says API tokens are not available to LDAP or SAML-created users; a local Threat Visualizer user is required to create a per-user pair. Prefer per-user tokens and least privilege over global tokens.

## Setup guidance

1. In Darktrace Threat Visualizer, grant the intended local user API Access through **Main Menu > Admin > Permissions Admin > Created Accounts** and the user's **Flags** step.
2. As that local user, open **Account Settings**, choose **API Access**, select **New**, and record both the public and private values securely. The private value is not displayed again.
3. Enter the host, public token, and private token in the Crogl connector setup form. Do not put credentials in `SKILL.md`, shell scripts, tickets, or chat.
4. For an appliance using a self-signed certificate, enable `skip_tls_verification` only when that is an explicit deployment requirement.

The supplied documents describe these menu labels for Threat Visualizer 5.1/6.1-era interfaces. Verify the labels against the customer's Darktrace version before provisioning.

## Safe query path

Use the CLI's bounded commands:

```bash
darktrace.py query-model-alerts --starttime <13-digit-ms> --endtime <13-digit-ms> --limit 25
darktrace.py query-incidents --starttime <13-digit-ms> --endtime <13-digit-ms> --limit 25
darktrace.py list-devices --limit 25
darktrace.py view-result --ref <ref> --row 1
```

The CLI caps rendered and cached rows at 100 after the upstream response is returned. The supplied Darktrace documents do not specify a pagination or result-window parameter for these three base query examples; engineering should add documented upstream pagination behavior if available.

The knowledge-graph commands are registered for the standard bundle interface, but live field discovery and sample learning remain unverified until a signed request can be made against a real instance.

Darktrace documents Unix timestamps in milliseconds for `starttime` and `endtime`. Supply them as a pair. Prefer short windows in busy environments because shorter queries return faster and avoid unnecessarily large responses.

## Endpoint catalog

### Model Alerts (`/modelbreaches`)

The supplied documentation describes this as the primary model-alert endpoint. Useful documented query parameters include:

- `starttime`, `endtime` -- paired epoch milliseconds.
- `includeacknowledged` -- include acknowledged alerts when needed.
- `includebreachurl` -- include a breach URL when the instance has FQDN configuration and the response is non-minimal.
- `minimal` -- reduce response size.
- `minscore` -- fractional threshold from 0.0 to 1.0.
- `did` -- device ID.
- `pbid` -- model alert ID.
- `pid` or `uuid` -- model identifier.
- `saasonly`, `saasfilter` -- identity/cloud filtering options documented by Darktrace.
- `creationtime` -- when true, filter time parameters by alert creation time rather than first relevant activity.

The CLI currently exposes the required time window and bounded result count. Additional filters should be added only after the auth path is resolved and the exact request-signing behavior is tested.

The documentation notes that alert priority is returned only with non-minimal responses and cannot be used as an upstream filter. Model-alert score is a decimal in the range 0.0–1.0.

### AI Analyst incident events (`/aianalyst/incidentevents`)

Darktrace recommends ingesting incident events rather than only dynamic incident groups when creating records in a third-party system. Incident events are comparatively stable; incident groups can expand and merge. Use the group endpoint for current grouping context when needed.

Documented parameters include `includeincidenteventurl`, `includeallpinned`, `minscore`, `starttime`, `endtime`, and the group-severity filters `groupcritical`, `groupsuspicious`, and `groupcompliance`. `saasonly` can restrict results to identity/cloud-related activity.

The CLI queries incident events with a bounded time window. It does not attempt client-side incident grouping.

### Devices (`/devices`)

Darktrace refers to tracked assets—including network devices and user identities—as devices. The supplied documentation describes:

- `GET /devices` -- list devices.
- `GET /devices?seensince=<value>` -- devices observed since a specified time.
- `GET /devices?did=<id>` -- one device's information.
- `GET /devicesearch` -- custom device search.
- `GET /devicesummary?did=<id>` -- contextual device summary.
- `GET /endpointdetails?hostname=<host>&ip=<ip>&devices=true` -- whether tracked devices have connected to an external endpoint.

The current CLI exposes the bounded base list only; the other routes remain documented extension points.

### Further investigation

The supplied documentation describes these read-oriented routes:

- `GET /details?pbid=<id>` -- event log for a model alert.
- `GET /details?did=<id>` -- event log for a device.
- `GET /advancedsearch/api/search/<base64-request>` -- advanced-search data using a base64-encoded request.
- `GET /pcaps` -- available packet captures.
- `GET /pcaps/<filename>` -- download a previously created capture.

PCAP creation and other state-changing operations are intentionally not exposed by this draft connector.

## SOC priority guidance

The supplied best-practices guide recommends prioritizing both AI Analyst incident events and Model Alerts. It describes these as a client-side baseline rather than a mandatory Darktrace behavior:

- **P1:** level-2 incident group with group score at least 30%; or Model Alerts tagged `Enhanced Monitoring`.
- **P2:** level-2 group below 30%; level-1 group at least 20%; or priority-5 Model Alerts with score at least 0.7.
- **P3:** level-1 group below 20%; level-0 groups; priority-5 score 0.5–0.7; or priority-4 score at least 0.7.
- **P4:** priority-5 score below 0.5; priority-4 score 0.4–0.7; or priority-3 score at least 0.7.
- **P5:** priority-4 score below 0.4; priority-3 score 0.5–0.7; or priority 0–2 with score at least 0.5.

P1 does not guarantee a true positive. Apply these rules only after retrieving the fields required for client-side filtering.

## Rendering and context limits

- Default to a narrow time window and at most 25 rows.
- Never request or render an unbounded historical result.
- Render model alerts with `pbid`, severity/category when present, model name, score, device identity, timestamps, and breach URL when available.
- Render incident events with event UUID, level, score, current-group identifiers, acknowledged state, related breach identifiers, and timestamps when present.
- Render devices with `did`, hostname or label, type, IP data, and first/last-seen values.
- Up to five rows can be shown in an inline table; for larger results show a compact sample and the remaining count.
- Use `view-result` for one cached row instead of repeating an identical upstream query.

## Error handling

- **401/403:** authentication, token validity, or insufficient Darktrace user permissions. Do not request that a user paste credentials into chat.
- **Date/signature rejection:** verify that the signed path/query exactly matches the request, that the same date was used in both places, that the date is UTC, and that the instance clock is within the documented 30-minute tolerance.
- **400:** malformed parameter, invalid timestamp pair, or unsupported endpoint parameter. Correct the request rather than retrying it unchanged.
- **TLS/certificate failure:** verify the host and certificate chain. Use `skip_tls_verification` only for an explicitly approved appliance exception.
- **5xx:** upstream Darktrace or appliance issue; report the status and retry only when appropriate.
- **Empty response:** a valid zero-result query may simply mean no matching alerts/events/devices. Report zero rows plainly.

## Guardrails

- This is a read-only draft. No acknowledgements, comments, tags, device edits, PCAP creation, or Autonomous Response actions are exposed.
- Never expose, log, reconstruct, or embed public/private token values.
- Do not use the sample token values shown in vendor documentation.
- Do not include the hostname in the HMAC input.
- Do not claim live compatibility until a real Darktrace instance has successfully answered a signed request.
- Do not infer API behavior beyond the supplied Darktrace documents; version differences must be confirmed with the customer's Darktrace technical contact.

## Required runtime environment

The CLI expects the standard Crogl container variables `CROGL_MCP_URL` and `CROGL_CLI_TOKEN`. Upstream Darktrace credentials remain server-side. Crogl's `darktrace_hmac` provider now supplies the required per-request signature server-side. Live compatibility remains unverified until a real Darktrace instance successfully answers a signed request.
