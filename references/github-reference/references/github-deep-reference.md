# GitHub Deep Reference (lazy-loaded)

Load this only when SKILL.md sends you here — when you need pagination mechanics, a scope-vs-permission table, the full Search qualifier list, or a worked write example.

## Pagination — Link header parsing

GitHub paginates list endpoints with a `Link` response header (RFC 5988). Example:

```
Link: <https://api.github.com/repos/o/r/issues?page=2&per_page=30>; rel="next",
      <https://api.github.com/repos/o/r/issues?page=12&per_page=30>; rel="last"
```

Four possible `rel` values: `next`, `prev`, `first`, `last`. `last` tells you how many pages exist for the current `per_page`.

**Rules:**

1. `per_page` is server-capped at 100 across REST endpoints (50 on a handful of older endpoints — Search API is 100).
2. `page` is 1-indexed.
3. **Never auto-paginate beyond page 1 unless the user explicitly asks for an exhaustive list.** A single follow-up call to page 2 is fine; looping until `next` is absent is not.
4. The Search API caps total results at **1000** regardless of `total_count` — pages past `total_count > 1000` return empty. Narrow the query if you hit this.
5. Some endpoints return a cursor in the URL instead of a page number (e.g. installations). For now: only paginate URL-from-`Link` verbatim; never construct the cursor by hand.

## Token scopes — classic PAT

Classic PATs use OAuth-style scopes. Common ones for this skill:

| Scope | Grants |
|-------|--------|
| `repo` | Full control of private repos: read/write issues, contents, commits, releases, deployments. **Includes `public_repo`.** |
| `public_repo` | Read/write issues + contents on public repos only. |
| `read:org` | Read org membership, teams, projects. Required for `/user/orgs` to return private org memberships, and for org-scoped Search API queries to see private repos. |
| `write:org` | Org admin actions — not needed for this skill. |
| `read:user`, `user:email` | User profile read — not needed for this skill. |

**Scope checks at runtime:** call `GET /user` then inspect the `X-OAuth-Scopes` response header. Comma-separated list of granted scopes. If empty (`X-OAuth-Scopes:`), the token is a "no-scope" classic PAT — usable for public reads only.

## Token scopes — fine-grained PAT

Fine-grained PATs (`github_pat_*`) don't use OAuth scopes — they use **per-resource permissions** chosen at token creation, with **per-repo or per-org allowlists**. The relevant permissions for this skill:

| Permission | Read | Write | Needed for |
|------------|------|-------|------------|
| Repository → Metadata | always Read | n/a | Required on every fine-grained token (implicit baseline). |
| Repository → Issues | Read | Read+Write | All issue endpoints. |
| Repository → Pull requests | Read | Read+Write | Surfaced in issue listings (PRs are issues); needed to filter or include. |
| Organization → Members | Read | n/a | `/user/orgs`, `/orgs/<org>/members`. |
| Organization → Administration | n/a | n/a | Not needed for this skill. |

Fine-grained PATs have **no `X-OAuth-Scopes` header**. If `X-OAuth-Scopes` is absent and the token works, it's almost certainly a fine-grained PAT. Diagnose missing permissions by the 403 response body ("Resource not accessible by personal access token") rather than by header inspection.

**Org allowlist:** a fine-grained PAT is scoped at creation to one resource owner (a user OR a single org). It cannot see repos in any other org regardless of permissions. If the user asks "list repos across orgs A and B" with a fine-grained PAT scoped to A, surface the limitation.

## Search API qualifier reference

Full list of qualifiers usable in `q=` for `/search/issues`. Always pair with `is:issue` and a `repo:`/`org:`/`user:` qualifier.

| Qualifier | Example | Note |
|-----------|---------|------|
| `is:issue` / `is:pr` | `is:issue` | Mandatory disambiguator. |
| `is:open` / `is:closed` / `is:merged` (PRs) | `is:open` | |
| `state:open` / `state:closed` | `state:closed` | Equivalent to `is:open`/`is:closed`. |
| `state_reason:` | `state_reason:completed`, `state_reason:not_planned`, `state_reason:reopened` | Closed-issue disposition. Available on closed issues only. |
| `repo:<owner>/<repo>` | `repo:octocat/hello-world` | |
| `org:<owner>` / `user:<owner>` | `org:crogl` | `org:` matches repos owned by the org; `user:` for user-owned repos. |
| `author:` | `author:octocat` | Login, not display name. |
| `assignee:<login>` / `assignee:none` / `assignee:*` | `assignee:octocat` | `*` = any. |
| `mentions:` | `mentions:octocat` | |
| `commenter:` | `commenter:octocat` | |
| `involves:` | `involves:octocat` | author OR assignee OR mentions OR commenter — the broad "anyone touched" filter. |
| `label:` | `label:bug` | Quote labels with spaces: `label:"needs triage"`. Multiple `label:` qualifiers AND together. |
| `milestone:` | `milestone:"v2.1"` | Title; quote if it contains spaces. |
| `no:label` / `no:milestone` / `no:assignee` | `no:label` | Filter to issues missing the attribute. |
| `created:` | `created:>=2026-01-01`, `created:2026-01-01..2026-01-31` | |
| `updated:` | `updated:>=2026-05-01` | |
| `closed:` | `closed:>=2026-05-01` | Closed-issues only. |
| `comments:` | `comments:>10`, `comments:0..5` | Count of comments. |
| `linked:pr` / `linked:issue` | `is:issue linked:pr` | Issues that have a linked PR (or vice versa). |
| `archived:true` / `archived:false` | `archived:false` | Hide archived repos. |
| `language:` | `language:python` | Repo's primary language. |
| `in:` | `in:title cmd.exe`, `in:body fallback`, `in:title,body foo` | Restrict free-text search to specific fields. |

Free text without a qualifier searches title + body by default.

**Boolean operators:** `AND`, `OR`, `NOT` — UPPERCASE only. Quote multi-word phrases.

**Special chars in qualifier values:** quote anything with spaces or special chars. Hyphens and dots in label names are fine unquoted. Colons must be quoted (`label:"foo:bar"`).

## Pivot map — issues

```
Issue ───number──→ Comments (/repos/{o}/{r}/issues/{n}/comments)
  │
  ├──assignees──→ User issues (assignee:<login> across org)
  ├──user.login──→ Authored by (author:<login>)
  ├──labels──→ Sibling issues (label:<L>, optionally per-repo)
  ├──milestone──→ Milestone members (milestone:"<title>")
  └──repository_url──→ Repo metadata (/repos/{o}/{r})

Search hit ───repository_url──→ One repo's issue list (/repos/{o}/{r}/issues)
                       └──→ Repo metadata
```

Carry the field value forward verbatim. Don't re-derive `<owner>/<repo>` by parsing `repository_url` if a sibling field already carries them broken out.

## Response field reference — issues list

The fields you'll actually use (full schema has 50+ fields per issue; these are the load-bearing ones):

| Field | Type | Note |
|-------|------|------|
| `number` | int | Per-repo issue number. Use in URLs. |
| `title` | string | |
| `body` | string\|null | Full markdown body. Can be 10s of KB — never carry forward verbatim across calls. |
| `state` | `"open"` \| `"closed"` | |
| `state_reason` | `"completed"` \| `"not_planned"` \| `"reopened"` \| null | Set on closed issues only. |
| `user.login` | string | Author. |
| `assignees[]` | array of users | Use `assignees[].login` for display. `assignee` (singular) is legacy — prefer the array. |
| `labels[]` | array of `{name, color, description}` | Use `labels[].name` for filter/display. |
| `milestone` | object\|null | `milestone.title`, `milestone.number`. |
| `comments` | int | Count, NOT the comments array. Fetch the array via `/issues/<n>/comments`. |
| `pull_request` | object\|absent | **Present → this is a PR, not an issue.** Use to filter. |
| `created_at`, `updated_at`, `closed_at` | ISO 8601 with `Z` | UTC. |
| `repository_url` | string | `https://api.github.com/repos/<o>/<r>` — parse if you need owner/repo broken out. Only set on cross-repo endpoints (`/issues`, `/search/issues`). |
| `html_url` | string | The github.com web URL — useful to surface to the user. |

## Worked example — create issue with labels and assignees

User: "open a P1 issue in `octocat/hello-world` titled `Server returns 500 on empty request body` assigned to `octocat`, tagged `bug` and `needs-triage`"

Before posting, **verify the labels exist** (don't auto-create):

```json
{"method": "GET", "path": "/repos/octocat/hello-world/labels", "query": {"per_page": "100"}}
```

Render the label list (or just confirm both `bug` and `needs-triage` are present). If either is missing, stop and ask the user whether to create the label first via `POST /repos/<o>/<r>/labels` body `{"name":"<L>","color":"<hex6>"}`.

Then ask the user to confirm the payload literally:

> About to create: repo `octocat/hello-world`, title `Server returns 500 on empty request body`, body `<empty>`, labels `[bug, needs-triage]`, assignees `[octocat]`. Confirm?

On confirmation:

```json
{
  "method": "POST",
  "path": "/repos/octocat/hello-world/issues",
  "body": "{\"title\":\"Server returns 500 on empty request body\",\"body\":\"\",\"labels\":[\"bug\",\"needs-triage\"],\"assignees\":[\"octocat\"]}",
  "headers": {"Content-Type": "application/json"}
}
```

Render the response: `#NNNN` · `https://github.com/octocat/hello-world/issues/NNNN`. Stop. Do not auto-add a comment, assign reviewers, link to anything, or open follow-ups.

## Worked example — close issue as not-planned with explanatory comment

User: "close `octocat/hello-world#1234` as not-planned and add a comment explaining we're going with the rewrite in #5678 instead"

This is **two write actions**. Render the proposed pair and require approval before executing.

> Plan:
> 1. Add comment to `octocat/hello-world#1234`: "<draft text>"
> 2. PATCH issue to `state=closed, state_reason=not_planned`
> Confirm both?

On confirmation, execute sequentially — comment first (cheap to retry if the close fails) then close. If the comment fails, stop and report — do not proceed to the close.

```json
{
  "method": "POST",
  "path": "/repos/octocat/hello-world/issues/1234/comments",
  "body": "{\"body\":\"Closing in favor of the rewrite — see #5678 for the replacement scope.\"}",
  "headers": {"Content-Type": "application/json"}
}
```

```json
{
  "method": "PATCH",
  "path": "/repos/octocat/hello-world/issues/1234",
  "body": "{\"state\":\"closed\",\"state_reason\":\"not_planned\"}",
  "headers": {"Content-Type": "application/json"}
}
```

## Rate-limit detail

Every response includes:

| Header | Meaning |
|--------|---------|
| `X-RateLimit-Limit` | Hourly cap (5000 authenticated; 60 unauth; 30/min for Search API). |
| `X-RateLimit-Remaining` | Remaining in this window. |
| `X-RateLimit-Reset` | Epoch seconds when the window resets. |
| `X-RateLimit-Used` | Used in this window. |
| `X-RateLimit-Resource` | Which bucket (`core`, `search`, `graphql`, `integration_manifest`, `code_scanning_upload`). |

**Search and core are separate buckets.** A burst of search calls won't deplete the core budget and vice versa.

**Secondary rate limits** (anti-abuse) return 403 with body `You have exceeded a secondary rate limit` and a `Retry-After` header (seconds). Triggered by: too many concurrent requests, too-rapid sequential identical requests, or volume-based heuristics GitHub doesn't document precisely. The skill's default `per_page=30` + no-auto-paginate posture avoids these in practice. If you hit one, stop and tell the user — never retry inside the `Retry-After` window.

## GitHub Enterprise Server differences (when `base_url` ends with `/api/v3`)

| Difference | Note |
|------------|------|
| `state_reason` on closed issues | Available 3.4+; older builds return `null` unconditionally. |
| Fine-grained PATs | 3.10+ only; earlier builds have classic PATs only. |
| Search API qualifiers | Most subset of github.com qualifiers; `linked:pr`/`linked:issue` available 3.6+. |
| Secondary rate limits | Configured per-instance; some installs disable them entirely. |
| `X-GitHub-Api-Version` header | Honored 3.7+. |

If the user's install is Enterprise Server and a specific endpoint behaves unexpectedly, surface the version (`GET /meta` returns `installed_version` on Enterprise Server but `404` on github.com — useful disambiguator).

## GraphQL — out of scope, here's the pointer

GraphQL endpoint is `POST /graphql` body `{"query":"<gql>"}`. Auth and rate limiting are independent of REST (separate `graphql` bucket).

When to consider GraphQL instead of this REST skill:

- You need a single issue + all its comments + linked PRs + reactions in one round trip (GraphQL does this in one query; REST takes 3–4 calls).
- You need fields the REST API doesn't expose (e.g. `timelineItems`, project board state, linked PR list).

For now: if the user explicitly asks for GraphQL, stop and recommend a dedicated `github-graphql-reference` skill rather than improvising against `/graphql`. GraphQL's query construction is non-trivial enough that ad-hoc composition by this skill would produce a lot of malformed queries.
