# ADR 0004: GitHub Issues is the canonical tracker; Linear is transitional

Status: accepted (2026-08-07)

New work for this repo is filed on this repo's issue tracker via `gh`. A second tracker, if any, is that deployment's configuration: search it for duplicates, write to it only on request, and do not migrate or close its issues unilaterally. Historical `EMB-1`…`EMB-54` identifiers are read-only. Existing `RAV-`/`EMB-` identifiers in code comments and migration strings are historical provenance and stay as they are. embry0's Linear and GitLab label-trigger integrations are product features and are unaffected by this.

The surface table lives in [`docs/agents/issue-tracker.md`](../agents/issue-tracker.md). The 2026-08-07 decision to make this repo's issue tracker canonical was recorded in `ba82720` (2026-08-21, #202) when the engineering-skills configuration was scaffolded.
