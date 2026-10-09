---
name: commit-standard
description: embry0's git commit standard. Use this skill EVERY time you write a commit message — Conventional Commits with a subject of at most 72 characters and a body wrapped at 72, distilled from Chris Beams' seven rules and anchored to embry0's existing scope vocabulary.
---

# embry0 Commit Standard

Adapted from a Conventional Commits house style. The shape is **Conventional Commits**: a subject of at most 72 characters, a body wrapped at 72 columns, and embry0's house scopes. `CONTRIBUTING.md` states the same limits.

---

## The shape

```
<type>(<scope>): <imperative summary>
                                                       ← blank line
<body — wrap 72, lead with why, then what, then notes>
                                                       ← blank line (if footers follow)
<footer-key>: <footer-value>
```

### Subject — `<type>(<scope>): <imperative summary>`

| Rule | Detail |
|---|---|
| `<type>` | One of `feat fix refactor perf docs test style chore build ci revert` — lowercase |
| `(<scope>)` | Optional. Lowercase, kebab-case. See scope vocabulary below |
| `: ` | Colon + single space |
| `<imperative summary>` | Imperative mood (`add` not `added`). Lowercase first letter. **No trailing period.** |
| Length | Subject ≤ 72 chars, the one limit (GitHub truncates a longer subject). A squash merge appends ` (#N)`, so keep a PR title short enough to stay within 72 with it |
| Banned | Emoji prefixes (gitmoji), AI footers without consent, multi-clause subjects with " and " (split into two commits) |

### Body

- Wrap at 72 columns
- Lead with **why** (the user-facing problem), then **what** (the change), then **notes** (gotchas, alternatives considered, follow-ups)
- Plain prose. No marketing language ("powerful", "stunning", "best-in-class")
- Reference related issues / design docs by issue number or in-repo path

### Footers

| Footer | When to use |
|---|---|
| `Refs: #N` | References issues / PRs the change relates to |
| `Fixes: #N` | Closes issue N when this PR merges |
| `BREAKING CHANGE: <description>` | API/contract change. Required for major-version bumps |
| `Co-Authored-By: <Name> <email>` | Genuine co-authorship only. **NOT** for AI tools unless the user explicitly asks |

**Banned footers**: `🤖 Generated with Claude Code`, `Co-Authored-By: Claude <noreply@anthropic.com>` (default — only with explicit user opt-in)

---

## embry0 scope vocabulary

The scope is the closest meaningful unit. Use these existing scopes from embry0's git history (per `git log --oneline`):

| Scope | Use when touching |
|---|---|
| `qa` | `embry0/workflows/qa/`, qa-related agent seed, qa pipeline graph |
| `api` | `embry0/api/` routes and middleware |
| `agents` | `embry0/agents/` + `embry0/storage/repositories/agent_definitions.py` |
| `frontend` | `frontend/` outside the narrower scopes below |
| `sandbox` | `embry0/sandbox/` + sandbox image / Dockerfile.sandbox |
| `storage` | `embry0/storage/` (migrations, repositories) |
| `proxy` | `embry0/execution/proxy/` |
| `execution` | `embry0/execution/` outside the proxies |
| `safety` | `embry0/safety/` (Ring 2 and Ring 3 policy) |
| `security` | Security hardening not owned by one scope above (API middleware, reserved env vars) |
| `infra` | `infra/` (compose, Dockerfiles, deploy script) |
| `watcher` | The log watcher and its healing loop |
| `notifications` | `embry0/notifications/` (outbound channels) |
| `triage` | The triage workflow node |
| `cli` | `embry0/cli/` |
| `integration` | `tests/integration/` |
| `stats` | `embry0/api/v1/stats.py` + stats response shape |
| `dashboard` | `frontend/src/pages/DashboardPage.tsx` + dashboard components |
| `pipeline-editor` | `frontend/src/components/pipeline-editor/` |
| `divine` | `frontend/src/components/divine/` |
| `layout` | `frontend/src/components/layout/` (AppLayout, TopBar, Sidebar) |

Skip the scope only when the change is genuinely repo-wide (e.g. `chore: bump dependency major across the stack`).

---

## Type taxonomy (embry0 practice)

| Type | Use when | Example |
|---|---|---|
| `feat` | New behavior, capability, or surface visible to a user/caller | `feat(stats): live state + per-repo + top-expensive + recent_issues` |
| `fix` | Bug fix — observable wrong behavior is now right | `fix(qa): destroy sandbox before cleanup_qa_resources removes the qa-net` |
| `refactor` | Internal restructure with **no behavior change** | `refactor(watcher): scope the watcher's tables to one stack` |
| `perf` | Performance improvement | `perf(triage): cache pipeline_template lookups` |
| `docs` | Documentation only | `docs: state the threat model that actually holds` |
| `test` | Test additions/fixes only | `test(integration): Phase 5 — QA gate routing smoke` |
| `style` | Formatting/whitespace, no logic change | `style: ruff --fix` |
| `chore` | Maintenance, deps, tooling | `chore: add high-standard PR template for the contribution loop` |
| `build` | Build system, Docker, CI infra | `build(sandbox): bundle vibium binary` |
| `ci` | CI workflow changes | `ci: add type-check to lint job` |
| `revert` | Revert a prior commit | `revert: feat(divine): pulse intensity tied to job count` |

---

## Anti-goals (matches embry0's existing anti-goal style)

- **No bare `chore: misc`** — Scope is optional only when the change is genuinely repo-wide. Otherwise pick a real scope.
- **No multi-clause subjects with " and "** — Split into two commits. One concept per commit.
- **No WIP commits on shared branches** — Squash locally before push.
- **No emoji** in any commit message.
- **No promotional language** in the body — pragmatic, fact-based, lead with why.
- **No AI co-author footers** unless the user explicitly opts in.

---

## Quick reference: subject patterns from embry0's existing history

```
fix(qa): destroy sandbox before cleanup_qa_resources removes the qa-net
test(integration): Phase 5 — QA gate routing smoke
docs: bring architecture.md + README up to speed with the QA agent
docs: enterprise-scale roadmap note
chore: add high-standard PR template for the contribution loop
feat(pipeline-editor): auto-arrange via dagre
feat(healing,watcher): ledger writers, cap on open unaccepted cases
refactor(qa,agents): extract browser wiring into a backend registry
```

Multi-scope commits (comma-separated) are valid when the change inherently spans related areas — e.g. `refactor(qa,agents)` because the browser wiring moved out of the QA workflow into an agent-side registry.

---

## How to use this skill

When you're about to write a commit message:

1. **Pick the type** — does this commit add user-visible behavior (`feat`), fix wrong behavior (`fix`), or restructure without changing behavior (`refactor`)?
2. **Pick the scope** — what's the smallest meaningful unit being touched? Use one from the table above.
3. **Write the subject in imperative mood** — "add", not "added"; "fix", not "fixes".
4. **Add a body if the why isn't obvious from the subject alone** — wrap at 72.
5. **Add footers only if needed** — `Refs:`, `Fixes:`, `BREAKING CHANGE:`.
6. **No emoji. No AI co-author footer. No "and" in the subject.**

---

*Adapted from a Conventional Commits house style. Per-repo `CONTRIBUTING.md` always wins over this skill.*
