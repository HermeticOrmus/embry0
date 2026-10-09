"""Deterministic verification for issue-to-PR jobs (#298, #299).

Two independent pieces, both free of LLM spend:

- ``gate``  — the orchestrator runs the repo's configured test commands on
  the base commit and on the job's head inside the dev sandbox and compares
  the two. Only failures that are NEW on head block the job.
- ``tiers`` — a pure classifier over the changed-file list that decides how
  much verification a change needs (``plan`` turns the tier into routing).

Configuration is per repo and lives in the embry0 database
(``repo_preferences.verification``) — never in a file of the target repo.
"""
