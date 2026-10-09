# Issue tracker: GitHub

Issues and specs for this repo live in **this repo's issue tracker** (GitHub
Issues). Use the `gh` CLI for all operations. A second tracker, if any, is
that deployment's configuration.

> **A second tracker is optional.** Search it for duplicates when one is
> configured. Treat this repo's issue tracker as authoritative for anything
> new, and do not migrate or close issues on the second tracker unless asked.

## Where things live

| Surface | Status | Use for |
|---|---|---|
| This repo's issue tracker (GitHub Issues) | **Canonical** | All new work. |
| A second tracker, if any | Deployment configuration | Search for duplicates. Write only when that deployment says to. |
| Historical `RAV-` and `EMB-` ids | Provenance | `EMB-1`…`EMB-54` and existing `RAV-` refs stay. Never file new work against them. |

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc or `--body-file` for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

`gh` infers the repo from `git remote -v` when run inside a clone.

Repo-specific:

- Titles use the repo's commit-style prefixes and scope vocabulary (`feat(qa):`, `fix(api):`, `chore:` …) — see the `commit-standard` skill.
- **Search this repo's issue tracker and a second tracker, if any, for duplicates** before creating an issue. The same work can already exist on either side.
- Reference tickets by `#<number>` for GitHub, or by the `RAV-`/`EMB-` identifier for Linear.
- Existing `RAV-####` and `EMB-##` references in code comments and migration `COMMENT ON COLUMN` strings are **historical provenance, not links to maintain**. Leave them.

## Epics

GitHub has no epic primitive, but it does have **native sub-issues**, which are
stronger than a checklist: closed children still count toward the parent's
completion, and GitHub computes the percentage itself.

- **Create the parent**, then link each child by its numeric **database id**
  (`gh api repos/<owner>/<repo>/issues/<n> --jq .id` — not the `#number`, not
  the `node_id`):
  `gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`
- **Read progress**: `gh api repos/<owner>/<repo>/issues/<parent> --jq .sub_issues_summary`

See #187 (`epic: GitLab forge integration`) for a worked example.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

## When a skill says "publish to the issue tracker"

Create a GitHub issue (`gh issue create`).

## When a skill says "fetch the relevant ticket"

Resolve `#<n>` with `gh issue view <n> --comments`. Resolve a historical
`RAV-<n>` or `EMB-<n>` identifier against the tracker that deployment still
keeps for old ids. `EMB-1`…`EMB-54` are read-only history.

Note that GitHub shares one number space across issues and PRs, so a bare
`#42` may be either: resolve with `gh pr view 42` and fall back to
`gh issue view 42`.

## Not to be confused with: the Linear label trigger

embry0's **product** integration with Linear — and now GitLab — is separate
from where embry0's own issues are tracked. Labelling an issue `embry0` in a
mapped Linear project triggers the pipeline against the mapped target repo
(EMB-47, rewired on 2026-07-23 by RAV-1023). See `LINEAR_REPO_MAP` and
`GITLAB_REPO_MAP` in `embry0/config.py`.
