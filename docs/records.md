# Repo records

Some documents about a repository must not live in its git tree, or in any git
tree: a root-cause analysis that names a customer, an audit that lists hosts, a
runbook with a deployment's addresses, a spec written for one installation.
embry0 keeps them itself, per registered repository, with full revision
history. They are called **records**.

- Operators write records through the API, the CLI or the MCP tools.
- The dashboard's **Records** page shows them, rendered, with every revision.
- Agents running a job read the records of the job's own repository, and of no
  other repository. They cannot write them.

Design and reasoning:
[`docs/superpowers/specs/2026-10-14-repo-records-design.md`](superpowers/specs/2026-10-14-repo-records-design.md).

## What a record is

| Field | Meaning |
|---|---|
| repository | A registered repository (`owner/name`, or `group/subgroup/name` on GitLab). Register it first. |
| slug | The record's address within the repository: lowercase letters, digits, `.`, `_`, `-`, starting with a letter or digit, at most 128 characters. |
| kind | `rca`, `audit`, `spec`, `plan`, `spike`, `runbook` or `note`. Fixed when the record is created. |
| title | Defaults to the body's first `# ` heading, then to the slug. |
| body | Markdown, at most 512 KiB. |
| revisions | Every write that changes the title or body adds a revision, numbered from 1. Writing the same title and body again adds nothing. |

A repository that has records cannot be deleted from the registry: delete its
records first (admin only), so an RCA never disappears as a side effect.

## CLI

The `embry0 records` commands talk to the running orchestrator. They find it
and authenticate the way `embry0 onboard` does: `--api-url` or
`EMBRY0_API_URL` (default `http://localhost:8200`), and `--api-key`,
`EMBRY0_API_KEY`, `API_KEY` or the `API_KEY=` line of `./.env`.

```bash
embry0 records list --repo acme/widgets [--kind rca] [--query cache] [--json]
embry0 records get outage-2026-10 --repo acme/widgets [--revision 2] [--json]
embry0 records put outage-2026-10 --repo acme/widgets --kind rca --file outage.md [--title "..."]
embry0 records import ./notes --repo acme/widgets [--dry-run]
```

### Importing a directory

`import` writes one record per `*.md` file under the directory. The file's
first directory picks the kind:

| Path under the directory | Kind |
|---|---|
| `audits/` | `audit` |
| `rca/` | `rca` |
| `spikes/` | `spike` |
| `ops/` | `runbook` |
| `superpowers/specs/` | `spec` |
| `superpowers/plans/` | `plan` |
| anything else | `note` |

- `README.md` files are skipped at any depth: they describe their directory.
- The slug is the file name without `.md`, lowercased, with every run of other
  characters replaced by `-` (`2026-09 Cache Outage.md` becomes
  `2026-09-cache-outage`).
- Two files that would get the same slug are both reported and neither is
  written. A slug that already exists with another kind is reported as a
  conflict.
- Running the import again is safe: an unchanged file adds no revision, a
  changed one adds exactly one.
- Each file gets one output line (`created`, `updated`, `unchanged`,
  `conflict`, `failed`, `skipped`); the exit code is 1 when any file was not
  written.

After a successful import, delete the source directory yourself: the import
never touches it.

## API

All under `/api/v1`, with the usual authentication; mutations need the
`X-Requested-With: XMLHttpRequest` header.

```bash
# List (newest first); optional kind, q (text in title or body), limit
curl "http://localhost:8200/api/v1/repos/acme/widgets/records?kind=rca" \
  -H "Authorization: Bearer $KEY"

# One record: the current revision, or ?revision=N
curl http://localhost:8200/api/v1/repos/acme/widgets/records/outage-2026-10 \
  -H "Authorization: Bearer $KEY"

# Revision history (metadata only)
curl http://localhost:8200/api/v1/repos/acme/widgets/records/outage-2026-10/revisions \
  -H "Authorization: Bearer $KEY"

# Create or revise: 201 on create, 200 otherwise; "changed" says whether a revision was added
curl -X PUT http://localhost:8200/api/v1/repos/acme/widgets/records/outage-2026-10 \
  -H "Authorization: Bearer $KEY" -H "X-Requested-With: XMLHttpRequest" \
  -H "Content-Type: application/json" \
  -d '{"kind": "rca", "body": "# Cache outage\n\n..."}'

# Delete a record and its history (admin)
curl -X DELETE http://localhost:8200/api/v1/repos/acme/widgets/records/outage-2026-10 \
  -H "Authorization: Bearer $KEY" -H "X-Requested-With: XMLHttpRequest"
```

| Who | May |
|---|---|
| read-scope keys and every other tier | list and read |
| operator tier | also create and revise |
| admin tier | also delete |
| a key limited to a repository allowlist | all of the above for its own repositories only, reads included |

Errors: 404 for an unregistered repository or a missing record or revision,
409 for a kind change, 413 for a body over 512 KiB, 422 for an invalid slug or
kind.

## MCP

`record_list(repo, kind?, query?)`, `record_get(repo, slug, revision?)` and
`record_put(repo, slug, kind, body, title?, confirm)`. `record_put` needs
`confirm=true`. There is no delete tool.

## What agents see

Every agent of an issue-to-PR job whose repository is registered gets two
read-only tools, `mcp__records__records_list` and `mcp__records__records_get`,
for that repository's records. They reach the records through the
`records-proxy`, which takes the repository from the sandbox's enrollment, not
from anything the sandbox sends, so an agent working on one repository cannot
read another's records. QA and onboarding jobs do not get the tools yet.

**Anything filed as a record of a repository is visible to the agents working
on it, and through them to the model provider those agents use.** Records are
not redacted. File material agents must not see somewhere else.

The tools need the `records-proxy` (in the proxy image) and the in-sandbox
server (in the sandbox images). Until both images are rebuilt after an
upgrade, jobs run without the tools and nothing else changes; the
orchestrator logs `records_proxy_enroll_failed` or
`records_proxy_scope_unconfirmed`. Rebuild recipes:
[`docs/ops/deploy.md`](ops/deploy.md#images-registry-pins-and-rebuilds).
