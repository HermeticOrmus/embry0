---
name: Work item
about: A planned change with acceptance criteria — plan items, hardening, follow-ups
labels: needs-triage
---

<!--
For plan items and engineering follow-ups, where the issue is the spec someone
(or an agent) will build from. The prompts below exist because each one has been
got wrong on a real issue in this repo — see the bullets under each heading.
-->

## Problem

<!-- What is wrong today, in behaviour terms. -->

## Measured behaviour today

<!--
NOT what you expect it does — what you observed it do. Run the cases and record
the results, ideally as a table.

Why: #356 stated that ten unparseable review shapes all routed `approved`.
Measured, three of them CRASHED the router with an uncaught AttributeError. The
issue was written from prediction, and the defect was larger than described.
-->

| case | current behaviour |
|---|---|
|  |  |

## Adjacent code that already handles the neighbouring case

<!--
Name it, and say why it does not cover this. If nothing exists, say so explicitly.

Why: #358 asked for "the commit-and-push recovery that triage and review already
have", implying the developer had none. `_rescue_push_branch` already existed;
the real bug was four words wide — it refuses UNCOMMITTED work. Filling this in
while filing would have produced a much smaller, more accurate issue.
-->

## Outcome required (the invariant)

<!--
State what must be TRUE when this is done, not the mechanism. Put any preferred
mechanism under "Suggested approach" as non-binding.

Why: #358's criteria specified "one resumed attempt whose only job is to commit
and push" — an implementation. The right answer was an in-process git commit,
which is cheaper and cannot hit the turn cap again. Prescribed mechanisms get
followed past the point where they stop being right.
-->

## Suggested approach (non-binding)

<!-- Optional. Explicitly overridable by whoever builds it, with a stated reason. -->

## Preconditions

<!--
Credentials, unconfigured services, or other issues this depends on. If any part
needs infrastructure that does not exist yet, split it into its own issue rather
than burying it in a checklist.

Why: #286 listed seven alert rules. One needed Loki's ruler (not configured in
that stack) and the alerting channel needed a Telegram bot token (does not
exist). Neither was stated, so the issue read as seven rules and was actually
six plus two infrastructure projects — and could not be closed.
-->

- [ ] Depends on: #
- [ ] Needs credentials / infra:

## Acceptance criteria

<!--
Each one independently checkable. Include the falsification step — see the
"Prove a check can fail" guardrail in CLAUDE.md.
-->

- [ ]
- [ ] New tests are shown **failing on the pre-fix tree**, with their own assertion message
- [ ] Any check that is currently quiet is shown **capable of firing** (relax only its threshold)
- [ ] Full unit suite failure **set** matches `origin/main` (expected floor: 9 failed / 45 errors with no test Postgres)

## How we will know in production

<!--
The observable that changes once this is deployed, and who checks it. Closing on
merge is not the same as working.

Why: four of the closed-loop plan's six success metrics sat at 0% while the code
was already shipped, and #274 / #275 / #278 / #301 stayed open purely because
their ops verification step was never recorded anywhere.
-->

- Observable:
- Checked by:

## Behaviour change to call out

<!--
Anything that will look like a regression to someone watching: new pauses, new
alerts, previously-silent failures becoming loud.

Why: the #356 fix converts up to 18 historically silent approvals into human
pauses, and the #286 rules fire immediately on a 56-day-old stalled case. Both
are correct and both look alarming if unannounced.
-->
