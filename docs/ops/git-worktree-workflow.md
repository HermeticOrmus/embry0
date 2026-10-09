# The Git Worktree Workflow — one-pager

**The rule this exists to enforce: do not check out another branch in the tree Compose builds.**
That directory is `<deploy-checkout>`. It must stay on the branch deploy
builds (`main` for this product) and match `origin/main` — never a
half-finished branch, never uncommitted edits. Worktrees live outside it,
at `<worktree-root>/<slug>`.

## What is a worktree? (from zero)

A git clone normally gives you ONE working directory; switching branches
changes the files in place. `git worktree` lets one clone check out
**several branches at once, each in its own directory**. They share one
`.git` history (no duplicate clones, instant creation), but files in one
directory never touch the others.

```
<deploy-checkout>            ← the tree Compose builds: ALWAYS on main
<worktree-root>/
  fix-login/                 ← worktree on branch alice/fix-login
  new-report/                ← worktree on branch bob/new-report
```

## The workflow (create → branch → PR → merge → prune)

```bash
# 0. One-time: pick a home for worktrees OUTSIDE the deploy checkout
mkdir -p <worktree-root>

# 1. Start a task: fresh worktree on a fresh branch off origin/main
cd <deploy-checkout>
git fetch origin
git worktree add <worktree-root>/fix-login \
    -b alice/fix-login origin/main

# 2. Work there — edit, commit, repeat. The deploy checkout is untouched.
cd <worktree-root>/fix-login
...edit...
git add -A && git commit -m "fix(auth): handle expired session on login"

# 3. Push and open a PR
git push -u origin alice/fix-login
gh pr create --base main

# 4. After the PR merges: update the deploy checkout, then prune
cd <deploy-checkout>
git pull --ff-only origin main        # deploy step happens from here
git worktree remove <worktree-root>/fix-login
git branch -d alice/fix-login
```

## Rules

1. **Never commit, branch, or edit in `<deploy-checkout>`.** It moves only
   by `git pull --ff-only origin main`. If `git status` there is ever
   dirty, something went wrong — fix that first.
2. **One worktree per task, fresh off `origin/main`.** Don't reuse an old
   worktree for a new task; `git fetch` first so "main" means today's
   main, not last week's.
3. **Everything reaches main through a PR.** No direct pushes to main.
4. **Prune when done** (step 4) — stale worktrees accumulate confusion.

## Why this matters for AI-polling repos

Agents and deploy pollers read `<deploy-checkout>` assuming it
IS production. A branch checked out there, or uncommitted edits, silently
feeds the agent — and whatever it deploys or reviews — a state that exists
on nobody's PR. The worktree layout makes that impossible by construction.

## Handy commands

```bash
git worktree list          # every checkout of this clone and its branch
git worktree remove <dir>  # remove one (refuses if it has dirty files)
git worktree prune         # clean up records of manually deleted dirs
```

*embry0 itself is developed this way: `<deploy-checkout>` stays on the
branch Compose builds, and each issue is a worktree at
`<worktree-root>/<slug>`.*
