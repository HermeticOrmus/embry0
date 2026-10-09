# ADR 0003: qa.yaml v2 lives in the embry0 DB; legacy channels removed

Status: accepted (2026-08-06)

A target repo's QA configuration is a schema-v2 document stored in embry0's `repo_qa_configs` table, keyed by the `repos` registry, and written through `PUT /repos/{owner}/{repo}/qa-config`, the dashboard editor, the `qa_config_set` MCP tool, or onboarding. Target repos carry no config file. The former channels — the `repo-configs/` file store, the in-repo `.embry0/qa.yaml` fallback and `apps/<name>/.embry0/app.yaml` tree collection — are gone; a repo with no stored document fails QA with an explicit "no QA config stored in embry0" error. v1 survives only in the `embry0 migrate-qa-config` migrator.

`da57a86` (2026-08-05, #134, RAV-1251) added the repo registry and the DB store; `ff3c317` (2026-08-06, #137, RAV-1253) deleted the legacy channels and made the DB the only source. Schema: [`embry0/workflows/qa/qa_yaml_v2.py`](../../embry0/workflows/qa/qa_yaml_v2.py); reference: [`docs/qa-yaml-reference.md`](../qa-yaml-reference.md).
