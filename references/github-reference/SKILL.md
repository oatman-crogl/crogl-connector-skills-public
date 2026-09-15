---
name: github-reference
description: "PREFER THIS SKILL for ad-hoc queries and write actions against the GitHub REST API, focused on Issues with read-only coverage of Orgs and Repos when token scope permits: 'list open issues in repo X', 'issues assigned to user Y', 'search issues mentioning term Z across the org', 'open an issue in repo X titled T', 'close issue N in repo X', 'comment on issue N', 'add label L to issue N', 'list repos in org X', 'list orgs my token can see', 'get repo X metadata'. Composes GET/POST/PATCH against the GitHub Custom Connector at https://api.github.com and renders the response as a scannable analyst table. Write actions (create issue, update/close issue, add comment/label/assignee) are gated on explicit user approval naming the target repo and issue. Do NOT use for pull-request review/merge, Actions/workflow management, code-content reads, GraphQL queries, or repo admin actions — out of scope for v0.1.0. GitHub.com + GitHub Enterprise Cloud ONLY."
version: "0.1.0"
---

# GitHub — Reference / Issues-focused Query Cookbook

> **v0.1.0 (2026-06-05) — Initial release.** Issues-first scope (read + write) plus read-only coverage of Orgs and Repos when the token carries the relevant scope. REST API v3 (`https://api.github.com`) via a Custom Connector with `Authorization: Bearer {{secret}}` + a non-empty `User-Agent` header (both injected by the connector). Reflects GitHub API behavior as of `X-GitHub-Api-Version: 2022-11-28`. GraphQL (`POST /graphql`), Actions, code-content reads, and PR review/merge are intentionally out of scope — they belong in dedicated skills.

Cookbook style: no phase gates, no STOP gates, no workflow orchestration. Read the catalog, build the call, render the result, stop. Write actions (issue create/update/comment/label/assign) are gated on explicit user approval naming target repo + issue.

## Connector selection (run once at start)

1. Walk the available `mcp__crogld__*` MCP tools to find connector tools.
2. Pick the connector tool whose name contains `github`, `gh`, `github_issues`, `github-issues`, `github_api`, or `github-api`. Match on substring — installs name their connector descriptively (e.g. `Github_Issues`) and the literal MCP-tool prefix (`api_proxy_` today, may change) is implementation detail.
3. If multiple GitHub connectors are registered (e.g. one per org with different tokens), list them and ask which one to use before issuing the call.
4. If none matches, tell the user "no GitHub connector is registered on this install — register one with `base_url=https://api.github.com`, an `Authorization: Bearer {{secret}}` header (storing a PAT or fine-grained token), and a non-empty `User-Agent` header before retrying." Do NOT attempt to construct `api.github.com` requests without a connector.
5. Cache the connector tool name. Invoke it with `{ method, path, query?, body?, headers? }`.

**GitHub.com is multi-tenant by org but the token defines the scope.** There is no `org_key` to inject — the token already implicitly scopes every call to whatever orgs/repos the issuing user is a member of and the token's scopes permit. Never ask the user for an `org_id`, `tenant_id`, or API ID.

For GitHub Enterprise Server installs, the connector `base_url` will be `https://<hostname>/api/v3` instead of `https://api.github.com`; same auth shape, but a small subset of endpoints (search filters, fine-grained PAT-specific responses) may differ. Flag those when you hit them.

## Auth recap

- Single header: `Authorization: Bearer <token>` (injected by the connector — never construct or prompt for the token).
- GitHub accepts both classic PATs (`ghp_*`) and fine-grained PATs (`github_pat_*`) on the same header shape. Behavior differs on org access: classic PATs see every org the user is a member of; fine-grained PATs are explicitly per-org and per-repo with per-resource permissions.
- `User-Agent` header is **required by GitHub** (non-empty). The connector injects one — never construct or override. If you see a 403 with body `Request forbidden by administrative rules. Please make sure your request has a User-Agent header`, the connector's User-Agent template is misconfigured; surface that to the user and stop.
- Optional but recommended: `X-GitHub-Api-Version: 2022-11-28`. Most endpoints work without it; if a future endpoint diverges between versions, the connector header template can pin it.
- 401 → token invalid/expired. Stop and surface.
- 403 → see "Disambiguating 403" in error handling below; can mean rate limit, SAML SSO not authorized, or insufficient scope.
- SAML SSO orgs: classic PATs need to be authorized for the specific org via the GitHub UI (`Settings → Developer settings → Personal access tokens → Configure SSO`). If `X-GitHub-SSO: required;...` appears in the response header, that's the signal.

## Tool call shape (critical)

The connector tool splits URL into separate fields. **Never embed `?…` in `path`** — crogld URL-encodes the `path` value, so a `?` becomes `%3F` and GitHub returns 404 on the doubly-encoded path.

```json
{"method": "GET", "path": "/repos/<owner>/<repo>/issues", "query": {"state": "open", "per_page": "30", "sort": "updated"}}
```

For write endpoints with a JSON body:

```json
{"method": "POST", "path": "/repos/<owner>/<repo>/issues", "body": "{\"title\":\"<title>\",\"body\":\"<body>\",\"labels\":[\"bug\"]}", "headers": {"Content-Type": "application/json"}}
```

The `body` field is a JSON-encoded string, not a nested object — the connector forwards it verbatim. Always set `Content-Type: application/json` on writes.

## Context budget rules

1. **Default `per_page=30`** (GitHub's server default) on every list call. Bump to 100 (the hard server cap) only when the user explicitly asks for a fuller view or you need to enumerate a small-ish set in one shot. **Never iterate past page 1 unless the user explicitly asks for an exhaustive list** — most enterprise repos have 1000s of issues.
2. **Render ≤25 items inline.** Beyond that, truncate with the `total_count` (from Search API) or a count-with-more-available framing and offer to filter further.
3. **Hard cap: if any tool result exceeds 30 KB, STOP** and re-issue with tighter scope (a `labels=`, `assignee=`, `state=closed`, or date qualifier).
4. **Summarize and discard.** After rendering the table, drop the raw JSON from your reasoning. Issue bodies are unbounded — never carry the full `body` field forward across follow-up calls.
5. **Don't re-paginate.** If you've already fetched page 1, follow-ups should narrow the query, not page deeper into the same one.
6. **Rate limit awareness.** Every response carries `X-RateLimit-Remaining` and `X-RateLimit-Reset` (epoch seconds). If `Remaining` drops below 100, slow down and surface to the user before continuing a bulk operation. Search API has a **separate, much tighter** rate limit (30/min authenticated) — see search section.

## Endpoint catalog

### Issues — read

| Method | Path | Use |
|--------|------|-----|
| GET | `/repos/<owner>/<repo>/issues` | List issues in one repo. **Defaults to `state=open`** — pass `state=closed` or `state=all` explicitly when needed. Returns PRs too (filter client-side; see "PRs in issue endpoints" below). |
| GET | `/repos/<owner>/<repo>/issues/<number>` | One issue by number. `<number>` is the per-repo issue number, NOT the global node ID. |
| GET | `/repos/<owner>/<repo>/issues/<number>/comments` | Comments on one issue, oldest-first. |
| GET | `/issues` | Issues across **every** repo the authenticated user is involved in (assigned / mentioned / created / subscribed). Useful for "what's on my plate." |
| GET | `/orgs/<org>/issues` | Issues across all repos the authenticated user can see in an org. **Same involvement filter as `/issues`** — does NOT list every issue in the org; that's the search API. |
| GET | `/search/issues` | Cross-repo / cross-org issue search. Different envelope + tighter rate limit — see "Search API" section. |

### Issues — write (gate on explicit user approval naming repo + issue)

| Method | Path | Body shape | Use |
|--------|------|------------|-----|
| POST | `/repos/<owner>/<repo>/issues` | `{"title": "...", "body": "...", "labels": ["..."], "assignees": ["..."]}` | Create issue. `title` required; everything else optional. |
| PATCH | `/repos/<owner>/<repo>/issues/<number>` | `{"state": "closed", "state_reason": "completed"}` (or `"not_planned"`, or `"reopened"` to reopen) | Update issue (close/reopen, edit title/body, replace labels, replace assignees). **Note: `labels` and `assignees` here REPLACE the existing arrays — use the dedicated endpoints below to add without replacing.** |
| POST | `/repos/<owner>/<repo>/issues/<number>/comments` | `{"body": "..."}` | Add comment. |
| POST | `/repos/<owner>/<repo>/issues/<number>/labels` | `{"labels": ["L1","L2"]}` | **Add** labels (additive). |
| POST | `/repos/<owner>/<repo>/issues/<number>/assignees` | `{"assignees": ["login1","login2"]}` | **Add** assignees (additive). Max 10 assignees per issue. |
| DELETE | `/repos/<owner>/<repo>/issues/<number>/labels/<name>` | none | Remove one label. |

Never invoke a write endpoint without explicit user approval that names the target repo + issue number (or, for create, the repo + title). Discovery is safe; writes are not.

### Orgs — read (require `read:org` scope on classic PATs, or org membership permission on fine-grained PATs)

| Method | Path | Use |
|--------|------|-----|
| GET | `/user/orgs` | Orgs the authenticated user is a member of. Returns only orgs the token's scope can see. |
| GET | `/orgs/<org>` | Org metadata. Public orgs don't need scope; private members-only fields hidden without `read:org`. |
| GET | `/orgs/<org>/members` | Org members. Requires `read:org`. |

### Repos — read

| Method | Path | Use |
|--------|------|-----|
| GET | `/user/repos` | Repos the authenticated user has access to. Pagination: many enterprise users have 100s of repos — always `per_page=100` and consider `affiliation=owner` / `visibility=private` qualifiers. |
| GET | `/orgs/<org>/repos` | Repos in one org. Filter via `type=public|private|forks|sources|member|all` (default `all`). |
| GET | `/repos/<owner>/<repo>` | One repo's metadata. |

### Endpoints NOT to call from this skill (out of scope for v0.1.0)

| Path | Why excluded |
|------|--------------|
| `/graphql` | GraphQL is a different connector contract; this skill is REST-only. |
| `/repos/<owner>/<repo>/pulls` and `/pulls/*` | PR review/merge has different state model and approval semantics — needs a dedicated workflow skill. PRs that surface in `/issues` endpoints are read-fine; write actions belong elsewhere. |
| `/repos/<owner>/<repo>/contents/*`, `/git/blobs/*`, `/git/trees/*` | Code-content reads — context-budget hostile and a different use case (code review, not issue triage). |
| `/repos/<owner>/<repo>/actions/*` | GitHub Actions — different lifecycle model, separate scope. |
| `POST /user/repos`, `POST /orgs/<org>/repos`, `DELETE /repos/*` | Repo admin actions — destructive blast radius; never offered by this skill. |

If the user asks for any of the above, say so and stop — don't improvise against an out-of-scope endpoint.

## Query syntax — Search API (cross-repo / cross-org)

For "search across the org" or "find issues mentioning X anywhere", use `GET /search/issues?q=<qualifiers>`. The response envelope differs from `/repos/<owner>/<repo>/issues`:

```json
{"total_count": 1234, "incomplete_results": false, "items": [...]}
```

The `items` array is what you render; `total_count` is the count for truncation framing.

### Mandatory qualifiers

| Qualifier | Example | Note |
|-----------|---------|------|
| `is:issue` | `is:issue` | **Always include.** `/search/issues` matches issues AND PRs by default; without `is:issue` you'll render PRs in an issue list. |
| `repo:<owner>/<repo>` OR `org:<owner>` OR `user:<owner>` | `repo:octocat/hello-world` | Scopes the search. Without one of these, you're searching all of public GitHub — useful occasionally, almost never what the user means. |

### Common filter qualifiers

| Qualifier | Example | Note |
|-----------|---------|------|
| `state:` | `state:open`, `state:closed` | |
| `is:` | `is:open`, `is:closed`, `is:merged` (PRs only) | Same as `state:` for open/closed; `is:` accepts more values. |
| `author:` | `author:octocat` | Login, not display name. |
| `assignee:` | `assignee:octocat` or `assignee:none` | |
| `mentions:` | `mentions:octocat` | |
| `label:` | `label:bug`, `label:"needs triage"` | Quote labels containing spaces. |
| `milestone:` | `milestone:"v2.1"` | Title, quoted if it contains spaces. |
| `created:` | `created:>=2026-01-01`, `created:2026-01-01..2026-01-31` | ISO date (with or without time). |
| `updated:` | `updated:>=2026-05-01` | |
| `closed:` | `closed:>=2026-05-01` | |
| `sort:` (URL param, not qualifier) | `&sort=updated&order=desc` | Sort fields: `created` (default), `updated`, `comments`, `reactions`. |
| Free text | `failover OR fallback "high availability"` | Boolean operators `AND`/`OR`/`NOT` UPPERCASE; quote phrases. |

**Search rate limit is separate and tight: 30 requests/minute authenticated.** Never loop search calls — compose one query, evaluate, refine if needed.

## Query syntax — per-repo list (`/repos/<owner>/<repo>/issues`)

Different from Search API — uses query params, not `q=` qualifiers.

| Param | Values | Note |
|-------|--------|------|
| `state` | `open` (default), `closed`, `all` | |
| `labels` | comma-separated string | `labels=bug,needs-triage` — AND semantics (must have ALL). |
| `assignee` | login OR `none` OR `*` | `*` = any assignee. |
| `creator` | login | |
| `mentioned` | login | |
| `milestone` | milestone number OR `none` OR `*` | |
| `since` | ISO 8601 with `Z` | Returns issues updated at or after this time. Format `2026-06-01T00:00:00Z` — note the `Z` (unlike CB-EDR which strips it). |
| `sort` | `created` (default), `updated`, `comments` | |
| `direction` | `asc`, `desc` (default) | |
| `per_page` | 1–100 (default 30) | |
| `page` | 1-indexed | |

## Query recipes

| User intent | Call |
|-------------|------|
| "Open issues in `<owner>/<repo>`" | `GET /repos/<owner>/<repo>/issues?state=open&per_page=30&sort=updated` |
| "Closed issues in `<owner>/<repo>` from the last 30 days" | `GET /repos/<owner>/<repo>/issues?state=closed&since=<now-30d>T00:00:00Z&per_page=50` (then client-side filter on `closed_at` for an exact "closed in last 30d" — `since` filters on `updated_at`) |
| "Issue #<N> in `<owner>/<repo>`" | `GET /repos/<owner>/<repo>/issues/<N>` |
| "Comments on issue #<N>" | `GET /repos/<owner>/<repo>/issues/<N>/comments?per_page=30` |
| "Issues assigned to `<login>` across org `<org>`" | `GET /search/issues?q=is:issue+is:open+org:<org>+assignee:<login>&per_page=30&sort=updated&order=desc` |
| "Issues mentioning `<term>` across org `<org>`" | `GET /search/issues?q=is:issue+org:<org>+<term>&per_page=30&sort=updated&order=desc` |
| "Issues I'm involved in" (across all repos the token sees) | `GET /issues?filter=involves&state=open&per_page=30&sort=updated` |
| "Open an issue in `<owner>/<repo>` titled `<T>` with body `<B>`" *(write — gate)* | `POST /repos/<owner>/<repo>/issues` body `{"title":"<T>","body":"<B>"}` |
| "Close issue #<N> in `<owner>/<repo>` as completed" *(write — gate)* | `PATCH /repos/<owner>/<repo>/issues/<N>` body `{"state":"closed","state_reason":"completed"}` |
| "Close issue #<N> as not-planned" *(write — gate)* | `PATCH /repos/<owner>/<repo>/issues/<N>` body `{"state":"closed","state_reason":"not_planned"}` |
| "Reopen issue #<N>" *(write — gate)* | `PATCH /repos/<owner>/<repo>/issues/<N>` body `{"state":"open","state_reason":"reopened"}` |
| "Comment on issue #<N>: `<text>`" *(write — gate)* | `POST /repos/<owner>/<repo>/issues/<N>/comments` body `{"body":"<text>"}` |
| "Add label `<L>` to issue #<N>" *(write — gate)* | `POST /repos/<owner>/<repo>/issues/<N>/labels` body `{"labels":["<L>"]}` |
| "Assign `<login>` to issue #<N>" *(write — gate)* | `POST /repos/<owner>/<repo>/issues/<N>/assignees` body `{"assignees":["<login>"]}` |
| "List orgs my token can see" | `GET /user/orgs?per_page=100` — note: classic PATs need `read:org` to see private orgs the user is a member of; fine-grained PATs return only the orgs the token was scoped to at creation. |
| "List repos in org `<org>`" | `GET /orgs/<org>/repos?per_page=100&type=all&sort=updated` |
| "Repo metadata for `<owner>/<repo>`" | `GET /repos/<owner>/<repo>` |

**Accepted user date phrasings — convert before composing the call:**

| User says | Convert to (with `Z`) |
|-----------|-----------------------|
| "last 24 hours" / "last day" | `since=<now-24h>Z` (per-repo) OR `updated:>=<now-24h date>` (search) |
| "yesterday" | `created:<yesterday date>` or `since=<yesterday 00:00:00>Z` |
| "in May" / "May 2026" | `created:2026-05-01..2026-05-31` |
| "since the v2 release" / vague | Ask the user for a date; don't guess. |

**GitHub dates use ISO 8601 WITH the `Z` suffix** (different from CB-EDR which strips it). Search API also accepts date-only qualifiers (`created:>=2026-01-01`).

## Rendering pattern

Scannable tables, default columns:

- **Issues list:** `#number` · `state` · `title` (truncate to 60) · `labels` (joined) · `assignees` (logins, joined) · `updated_at` (date only)
- **Issue detail:** number, title, state (with state_reason if closed), author, assignees, labels, created_at, updated_at, comments count; then body preview (≤300 chars) and a "N comments — ask to see them" line if `comments > 0`
- **Comments:** `created_at` · `author` · `body` (truncate to 120)
- **Search hits:** same as issues list + a `repo` column (parsed from `repository_url`)
- **Orgs:** `login` · `description` (truncate 60) · `public_repos` · (`type` if mixed users/orgs)
- **Repos:** `full_name` · `description` (truncate 60) · `private` · `default_branch` · `updated_at`

≤25 rows → inline. >25 → show 25 + "(Showing 25 of `total_count`; ask to narrow or page)". Render once, then drop the raw response.

### PRs in issue endpoints

`/repos/<owner>/<repo>/issues` and `GET /issues` **return PRs alongside issues** — every PR is also an issue in GitHub's data model. A returned object whose `pull_request` field is present (non-null) is a PR, not an issue.

- **Per-repo list** (`/repos/<owner>/<repo>/issues`): **filter client-side** by dropping rows where `pull_request` is present, unless the user explicitly asked for both.
- **Search API**: use the `is:issue` qualifier (cheaper than fetching + filtering).
- If you find yourself rendering rows with `#NNN` numbers that turn out to be PRs, flag it ("3 of these are PRs — want me to filter them out?") rather than silently mixing.

## Error handling

- **400 / 422 `Validation Failed`** — POST/PATCH body is malformed or references a non-existent resource. Common: assignee login not in the repo's collaborators (some endpoints silently drop, others 422), label name doesn't exist on the repo, `title` missing on create. Surface the response body's `errors[]` array verbatim — it names the field and the failure mode.
- **401 `Bad credentials`** — token invalid or expired. Stop, ask user to refresh the connector's stored secret.
- **403 — disambiguate via response headers and body:**
  | Signal | Cause | Action |
  |--------|-------|--------|
  | `X-RateLimit-Remaining: 0` | Rate limited. Resets at `X-RateLimit-Reset` (epoch seconds). | Stop, tell user when the reset is, optionally suggest narrowing scope. |
  | Response body `Request forbidden by administrative rules. Please make sure your request has a User-Agent header` | Connector's User-Agent template is misconfigured / empty. | Stop, surface — this is a connector bug, not a token problem. |
  | `X-GitHub-SSO: required; url=...` in response headers | Token isn't authorized for this org's SAML SSO. | Stop, tell user to follow the URL in the header to authorize the token for the org. |
  | Body `Resource not accessible by personal access token` | Fine-grained PAT lacks the permission for this resource/action. | Stop, name the missing permission (e.g. "Issues: Write" for a write call). |
  | Body `Must have admin rights` or similar | Token's scope doesn't cover the action. | Stop, name the missing scope (`repo`, `read:org`, etc.). |
- **404** — resource doesn't exist OR token can't see it. **On a fine-grained PAT, 404 can mean "exists but not in your token's repo allowlist"** — don't tell the user the issue/repo doesn't exist if they're explicit about its name; instead say "either it doesn't exist or your token can't see it."
- **410 Gone** — issues are disabled on the repo. Surface and stop; the user has to enable issues on the repo settings before any issue endpoint will work.
- **422 on label add** — label doesn't exist on the repo yet (`labels` array contained a name not in the repo's label set). Don't auto-create the label — surface the failure and ask the user whether to create it first.
- **Secondary rate limits** (abuse detection, separate from primary rate limit) return `403 You have exceeded a secondary rate limit` with `Retry-After` header in seconds. Stop and tell the user — never retry in a tight loop.

For pagination details, scope tables, GraphQL pointers, and worked write examples: see [references/github-deep-reference.md](references/github-deep-reference.md).

## Guardrails

- **No write actions without explicit user approval naming the target** (repo + issue number for updates/comments/labels/assignees; repo + title for creates). Render the proposed payload, ask, then execute.
- **No bulk writes.** Even with approval, if the user asks "close all open issues with label `<L>`", refuse — list them first, confirm specific numbers, then execute one at a time. Bulk close-via-PATCH-loop is a footgun.
- **Render before pivoting.** Answer the ad-hoc question, then offer follow-ups — don't pre-emptively chain.
- **Respect token scope.** If the token can't see private repos in an org, don't speculate about their contents. Report what the token returns and name the scope gap if relevant.
- **Don't fabricate logins or label names.** Assignees must be existing collaborators; labels must exist on the repo. If unsure, fetch `/repos/<owner>/<repo>/labels` or `/repos/<owner>/<repo>/assignees` first to confirm.
- **GitHub.com vs Enterprise Server.** If the connector `base_url` ends with `/api/v3` (Enterprise Server), flag in the response: "this is GitHub Enterprise Server, some endpoints may differ from github.com."
