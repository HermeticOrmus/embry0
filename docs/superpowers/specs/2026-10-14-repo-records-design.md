# Repo records: design

Date: 2026-10-14. Status: slice 1 (the store) implemented on `feat/repo-records`.

## Problem

embry0 is developed in its public repository. Some of the documents written
while operating it name a customer, a deployment, a person, a host, a cost or a
job id: root-cause analyses, audits, spikes, deployment-specific specs and
plans, runbooks. They cannot live in the public tree, and moving them into a
second, private git repository only recreates the split that was just removed.

They need a home that is never a git tree: a store inside embry0 itself, keyed
by the registered repository the document is about, with full revision
history, that operators can write and that agents working on that repository
can read.

## Decisions

These were taken by the owner before design and are not re-opened here.

1. **Slice 1 is the store only.** Tables with full revision history, a REST
   API, operator MCP tools, an `embry0 records` CLI with an import command, a
   dashboard page and docs. Mirroring records to a tracker (a GitLab wiki,
   Linear documents, files in a private repository) is slice 2.
2. **Agents read, operators write.** An agent running inside a job reads the
   records of the job's own repository and no other. Writing stays with
   operators (API, CLI, MCP) and, later, embry0's own pipelines.
3. **The spec ships with the code**, in this branch.
4. **A one-time import** of a directory of Markdown files maps
   subdirectories to kinds and is idempotent.

## Model

A **record** is one Markdown document about one registered repository,
addressed by `(repo, slug)`. Its content is a sequence of **revisions**,
numbered from 1; the record's current content is its highest revision.

| Field | Where | Notes |
|---|---|---|
| `repo` | record | FK to `repos(repo)`. The repository must be registered. |
| `slug` | record | `^[a-z0-9][a-z0-9._-]{0,127}$`, unique per repo. |
| `kind` | record | One of `rca`, `audit`, `spec`, `plan`, `spike`, `runbook`, `note` (a CHECK constraint and a Python tuple, pinned against each other by a test). Fixed at creation. |
| `title` | record + revision | The record carries the current title; each revision keeps the title it was written with. |
| `current_revision` | record | Highest revision number. |
| `created_at`, `updated_at` | record | |
| `body` | revision | Markdown, at most 512 KiB of UTF-8 (`MAX_BODY_BYTES`). Not 1 MiB: nginx in front of the API keeps its default `client_max_body_size` of 1 MiB, and a JSON-encoded body needs headroom for its escapes, so a 1 MiB cap would be answered by nginx's HTML 413 instead of the store's clear one. |
| `author` | revision | `request_actor` of the writer (`key:<name>`, a user, `master`). |
| `source_job_id` | revision | Optional: the job that produced this revision. Nullable, no FK (jobs are pruned; the record must not be). |
| `created_at` | revision | |

### Writing

`put(repo, slug, kind, body, title=None, author, source_job_id=None)`:

- No record: create it at revision 1.
- Record exists with another kind: refused (409). Kind is fixed at creation; a
  record that changes kind is a new record.
- Record exists and the latest revision has the same title and body: no new
  revision, `changed: false`. This is what makes a re-import idempotent.
- Otherwise: append revision `current + 1` and move the record's title and
  `updated_at`.

The title defaults to the first `# ` heading of the body, then to the slug.
Revision numbers are assigned under a row lock on the record
(`SELECT ... FOR UPDATE`), so two concurrent writers cannot both write
revision `n`; the `(record_id, revision)` primary key backs that up.

An unregistered repository is refused with 404 rather than auto-registered:
registering a repository has to pick its forge, and a record write is no place
to guess one (the reasoning of `repo_qa_configs.upsert`, Spec 1A section 3.8).

### Deleting

`DELETE /repos/{repo}/records/{slug}` removes a record and all its revisions.
Admin tier only: history is the point of the store. Deleting a registered
repository that still has records is refused with 409 (`ON DELETE RESTRICT`),
so a repository delete never silently takes its RCAs with it. Delete the
records first.

### Why no remote ref column yet

A slice-2 mirror needs to remember where each record was mirrored (a wiki page
id, a document id, a path and blob sha). That shape depends on the mirror, so
the column is added with the mirror rather than guessed now. The store keeps
the two properties a mirror needs: a stable `(repo, slug)` address and a
monotonic revision number to compare against.

## Surfaces

### REST API

All under `/api/v1`, behind the usual API authentication and the CSRF header
on mutations. Repository paths take any depth (`group/sub/project`), as every
`/repos/{full_repo:path}` sub-resource does; the records router is included
before the repos catch-all and the route-order test covers `records`.

| Route | Tier | |
|---|---|---|
| `GET /repos/{repo}/records?kind=&q=&limit=` | read | List, newest first. `q` is a case-insensitive substring match on title and current body. |
| `GET /repos/{repo}/records/{slug}?revision=N` | read | The current revision, or revision `N`. |
| `GET /repos/{repo}/records/{slug}/revisions` | read | Every revision's metadata, newest first, without bodies. |
| `PUT /repos/{repo}/records/{slug}` | operator | JSON `{kind, body, title?, source_job_id?}`. 201 on create, 200 otherwise; the response says `created` and `changed`. |
| `DELETE /repos/{repo}/records/{slug}` | admin | 204. |

A key restricted to a repository allowlist (`api_keys.allowed_repos`) may use
these routes only for repositories on its list, reads included: records are
exactly the material such a key must not see for other repositories.

**Ingress.** The routes fall under "Every other `/api/` route" in
`docs/ops/deploy.md`: reachable through the dashboard tunnel and the LAN, not
the webhooks tunnel. Nothing new is exposed to the internet beyond what an
authenticated dashboard user already reaches.

### Operator MCP tools

`record_list(repo, kind?, query?)`, `record_get(repo, slug, revision?)`,
`record_put(repo, slug, kind, body, title?, confirm=false)`. `record_put`
requires `confirm=true`, like every other tool that writes platform state.
There is no MCP delete.

### CLI

`embry0 records list|get|put|import`, thin clients of the REST API in
`embry0/cli/records.py`, resolving the URL and key as `embry0 onboard` does.

`embry0 records import <dir> --repo <owner/repo> [--dry-run]` walks `<dir>`
for `*.md` files and maps the first matching directory prefix to a kind:

| Path under `<dir>` | Kind |
|---|---|
| `audits/` | `audit` |
| `rca/` | `rca` |
| `spikes/` | `spike` |
| `ops/` | `runbook` |
| `superpowers/specs/` | `spec` |
| `superpowers/plans/` | `plan` |
| anything else | `note` |

`README.md` files are skipped at any depth: in such a directory they describe
the directory, not a document. The slug is the file name without `.md`,
lowercased, with every run of characters outside `[a-z0-9._-]` replaced by `-`.
Two files mapping to one slug are reported and neither is written, rather than
one silently overwriting the other. A slug that already exists with a
different kind is reported as a conflict and skipped. Re-running the import
writes no new revision for an unchanged file. The command prints one line per
file (`created`, `updated`, `unchanged`, `skipped`, `conflict`) and exits
non-zero when any file failed.

### Dashboard

A **Records** page (`/records?repo=…&slug=…&rev=…`): a repository picker, the
list with a kind filter and search, the selected record rendered as Markdown,
and a revision picker. Read-only in slice 1; writing is the CLI's and the
MCP's job. The repository detail page links to it.

### Agents in jobs: the records-proxy

Agents reach orchestrator data through mediating proxies on the
`sandbox-restricted` network, never the orchestrator itself. The closest
precedent is the context-proxy (watcher log queries): it holds an orchestrator
key the sandbox never sees, authenticates each request by the per-sandbox
bearer enrolled by `ProxyManager`, and enforces a per-enrollment scope. The
forge API proxies add the repository scope (`allowed_repos`, #520).

Records reuse both patterns in a new proxy, **records-proxy** (port 9108),
rather than widening the context-proxy: the context-proxy is opt-in per job,
launched only when log access is configured, and its scope is a container
list. Folding records into it would couple an always-on read path to an
opt-in log feature.

- **Scope comes from the enrollment, not the request.** The sandbox asks for
  `GET /records` or `GET /records/{slug}`; it never names a repository. The
  proxy builds the upstream path from the repository enrolled for that
  sandbox, so there is no request a sandbox can make for another repository's
  records. A sandbox enrolled with an empty scope (a job with no repository)
  gets 403 on every call.
- **Read only.** Two GET routes, a query-parameter allowlist (`kind`, `q`,
  `limit`, `revision`), a slug regex. Everything else is 404 or 400.
- **Enrollment** rides the `enroll_sandbox` broadcast with
  `allowed_repos: [repo]`, so re-enrollment on resume and replay into a
  recreated proxy come for free. The proxy echoes the scope; a proxy that does
  not (an image older than the records-proxy) is unenrolled again.
  Unlike the git-proxy, a records-proxy failure never fails the sandbox:
  records are context, not a credential, and a job ran without them before.
- **Upstream credential.** A boot-rotated `read`-scope service key
  (`svc-records-proxy`), as the context-proxy uses. If the key cannot be
  rotated, the proxy is not launched: the master key never leaves the
  orchestrator for this.
- **In the sandbox** a stdio MCP server, `embry0/sandbox/records_mcp.py`,
  exposes `records_list` and `records_get` (as
  `mcp__records__records_list` / `mcp__records__records_get`). It reads
  `EMBRY0_RECORDS_PROXY_URL` and the bearer from a 0600 file in the agent's
  home, as the logs tool does. `init_node` delivers both and sets
  `records_tool` in the job state only when the proxy confirmed the
  enrollment; `run_agent_node` then adds the tools to every agent invocation
  of that job. `EMBRY0_RECORDS_PROXY_URL` is a reserved variable.

Record content is operator-written context for the repository the job works
on. It reaches the job's model provider like the repository's own files do.
It is not redacted: an operator who would not show a record to the job's
agents should not file it under that repository.

## Deploy

- Migration 95 (`repo_records`, `repo_record_revisions`).
- Orchestrator and frontend: the usual deploy.
- **Proxy image rebuild** (new `records-proxy` type) and **sandbox image
  rebuild** (new in-sandbox MCP server). Until both are rebuilt, agents get no
  records tool; nothing else is affected. Recipe: `docs/ops/deploy.md`,
  "Images: registry, pins and rebuilds".

## Testing

- Repository against Postgres: create, idempotent re-put, title-only change,
  kind conflict, body cap, revision fetch, search and kind filter, delete,
  concurrent puts, repository delete refused while records exist.
- API: shape, multi-segment repositories, route order, tiers, allowlisted
  keys, 404, 409, 413.
- MCP tools: confirm gate, URL encoding, parameters pinned in the tool-surface
  table.
- CLI import: kind mapping, README skip, slug rules, duplicate slugs,
  idempotency against a fake API.
- Proxy: scope from enrollment; a sandbox enrolled for repo A cannot read
  repo B by any path; empty scope refused; unknown parameters refused; the
  echo; the manager's fail-open broadcast.
- In-sandbox MCP server and the grant in `run_agent_node`.

## Open questions

1. **Revision retention.** Every changed write keeps a full copy of the body.
   At up to 512 KiB per revision a busy record grows without bound. No pruning in
   slice 1; revisit once real usage shows sizes.
2. **QA and onboarding sandboxes** do not get the records tool in slice 1:
   only the issue-to-PR pipeline's `init_node` provisions it. QA agents judge
   acceptance criteria from the ticket; whether they should also read RCAs is
   open.
3. **Writes from pipelines** (for example a watcher RCA filed as a record) are
   out of scope. When added they should pass `source_job_id`.
4. **Renamed repositories.** Records are keyed by the registered slug. A
   rename moves the registry row (see "Repo renames have three homes" in
   `CLAUDE.md`); records follow only if the registry row is renamed in place.
   `REPO_RENAMES` is not consulted on the records routes.
5. **Search** is a substring scan of current bodies. Enough for hundreds of
   records per repository; full-text search waits for a need.
