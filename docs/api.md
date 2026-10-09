# API Reference

Two-level API: **low-level graph execution** + **high-level job management**. All endpoints live under `/api/v1/` behind the frontend's nginx on port 8200.

Authentication: send `Authorization: Bearer <API_KEY>` on every request (omitted from the examples below for brevity). An empty `API_KEY` is only tolerated when a dev-mode flag is set — see [configuration.md](configuration.md).

See [architecture.md](architecture.md#api-structure) for the full route map.

## Issues

```bash
# Create an issue (with optional auto-triage and GitHub sync)
curl -X POST http://localhost:8200/api/v1/issues \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"title": "Fix the auth bug", "repo": "owner/repo", "auto_triage": true, "github_sync_enabled": true}'
# -> 201 with the issue plus "job_id", "dispatch_error_code" and "dispatch_error".
#    With auto_triage, a non-null "dispatch_error_code" ("ERR_DISPATCH_FAILED")
#    means the issue was created but NO job started (it is back in "open").
#    Check it — the status code is 201 either way, so a retry never creates the
#    issue twice. "dispatch_error" is a safe sentence; the cause is in the
#    orchestrator log. POST /issues/{issue_id}/triage retries and reports the
#    same three fields (status 200).

# List issues
curl http://localhost:8200/api/v1/issues -H "X-Requested-With: XMLHttpRequest"

# Get issue with children and jobs
curl http://localhost:8200/api/v1/issues/{issue_id} -H "X-Requested-With: XMLHttpRequest"

# Answer a triage question
curl -X POST http://localhost:8200/api/v1/issues/{issue_id}/inputs/{input_id}/answer \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"answer": "The bug is in the JWT validation logic"}'
```

## Jobs (high-level)

```bash
# Create a job
curl -X POST http://localhost:8200/api/v1/jobs \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"repo": "owner/repo", "task": "Fix the auth bug in login.py"}'

# List jobs
curl http://localhost:8200/api/v1/jobs

# Cancel a job
curl -X POST http://localhost:8200/api/v1/jobs/{job_id}/cancel \
  -H "X-Requested-With: XMLHttpRequest"

# Submit a QA job (skips triage; needs branch + pipeline=qa). See docs/running-qa.md.
curl -X POST http://localhost:8200/api/v1/jobs \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"repo": "owner/repo", "branch": "main", "pipeline": "qa",
       "qa": {"acceptance_criteria": ["home page loads with no console errors"]}}'

# QA artifact endpoints (presigned-GET redirects + SSE log stream).
curl http://localhost:8200/api/v1/jobs/{job_id}/qa/attempts
curl http://localhost:8200/api/v1/jobs/{job_id}/qa/attempts/1/result
curl http://localhost:8200/api/v1/jobs/{job_id}/artifacts/screenshots/latest
curl "http://localhost:8200/api/v1/jobs/{job_id}/artifacts/logs/gateway?follow=true"
```

## Graph execution (low-level)

```bash
# List available workflows
curl http://localhost:8200/api/v1/graphs/workflows

# Execute a workflow
curl -X POST http://localhost:8200/api/v1/graphs/execute \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"workflow": "issue-to-pr", "input_state": {"repo": "owner/repo", "task": "..."}}'
```

## Runtime configuration

```bash
# Budget controls
curl http://localhost:8200/api/v1/config/budget
curl -X PUT http://localhost:8200/api/v1/config/budget \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"max_budget_per_job_usd": 25.0}'

# Context injection
curl http://localhost:8200/api/v1/config/context
curl -X PUT http://localhost:8200/api/v1/config/context \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"system_context": "Use TypeScript strict mode."}'

# Sandbox profiles
curl http://localhost:8200/api/v1/sandbox-profiles
curl -X POST http://localhost:8200/api/v1/sandbox-profiles \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"name": "java-17", "base_image": "embry0-sandbox-java:17", "memory": "12g"}'
```

## Environment variables

```bash
# List global environment variables (secrets masked)
curl http://localhost:8200/api/v1/environment/global \
  -H "X-Requested-With: XMLHttpRequest"

# Set global environment variables
curl -X PUT http://localhost:8200/api/v1/environment/global \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"variables": [{"key": "DATABASE_URL", "value": "postgres://...", "var_type": "secret"}]}'

# Reveal a masked secret (audit-logged)
curl http://localhost:8200/api/v1/environment/global/DATABASE_URL/reveal \
  -H "X-Requested-With: XMLHttpRequest"

# Per-repo environment (same pattern, scoped)
curl http://localhost:8200/api/v1/repos/owner/repo/environment \
  -H "X-Requested-With: XMLHttpRequest"

# Auto-detect env vars from repo's .env.example
curl http://localhost:8200/api/v1/repos/owner/repo/environment/detect \
  -H "X-Requested-With: XMLHttpRequest"
```

Some keys are refused with a 422, matched case-insensitively. The same check drops them again at sandbox start, from stored rows and from profile `env_defaults`. These keys are the infrastructure credentials and proxy URLs, plus every variable that would redirect a sandbox's traffic or load code into its processes: proxy routing, TLS verification, the dynamic loader, `PATH`/`HOME`/`SHELL`, shell and interpreter startup hooks, and the `GIT_`, `CLAUDE_` and `BUN_` namespaces. `NODE_OPTIONS` and the JVM option variables (`JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS`, `JAVA_OPTS`, `MAVEN_OPTS`, `GRADLE_OPTS`) are accepted, but only with memory flags. The authoritative list is `embry0/execution/auth_provider.py`. A 422 on these routes never echoes the submitted value.

## Credentials

Forge accounts and their bindings ([`docs/credentials.md`](credentials.md)). Reads need any authenticated principal; every mutation needs the `admin` tier and writes a `credential.*` audit row. Secrets are write-only: no endpoint returns one, and there is no reveal endpoint.

| Endpoint | Tier |
|---|---|
| `GET /api/v1/credentials` | read |
| `POST /api/v1/credentials` | admin |
| `PUT /api/v1/credentials/{id}` | admin |
| `POST /api/v1/credentials/{id}/rotate` | admin |
| `POST /api/v1/credentials/{id}/verify` | admin |
| `DELETE /api/v1/credentials/{id}` | admin |
| `GET /api/v1/credentials/resolve?repo=&role=forge` | read |
| `GET /api/v1/credentials/bindings?scope_kind=&scope_value=` | read |
| `PUT /api/v1/credentials/bindings` | admin |
| `POST /api/v1/credentials/import-env` | admin |
| `GET /api/v1/repos/{repo}/credentials` | read |
| `PUT /api/v1/repos/{repo}/credentials` | admin |

```bash
# Create a GitHub account (the response carries masked_preview, never the secret)
curl -X POST http://localhost:8200/api/v1/credentials \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"kind": "forge_token", "provider": "github", "label": "alice", "secret": "github_pat_..."}'

# Bind every acme repo to it
curl -X PUT http://localhost:8200/api/v1/credentials/bindings \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"scope_kind": "owner", "scope_value": "github:acme", "bindings": [{"credential_id": "cred-0123456789ab", "role": "forge"}]}'

# Dry run: which account would a job on acme/widgets use, and why
curl "http://localhost:8200/api/v1/credentials/resolve?repo=acme/widgets&role=forge" \
  -H "X-Requested-With: XMLHttpRequest"

# Move GITHUB_TOKEN* / GITLAB_TOKEN* from the orchestrator env into the registry
curl -X POST http://localhost:8200/api/v1/credentials/import-env \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"replace": false}'
```

### Discovery

`GET /api/v1/github/discoverable-repos` and `GET /api/v1/gitlab/discoverable-projects` fan out across registry credentials for that forge and the env tokens, then drop repos already registered. Behavior is in [credentials.md](credentials.md). `refresh=true` bypasses the cache.

| Field | GitHub | GitLab |
|---|---|---|
| `credential_id` | registry id, or null for an env token | same |
| `credential_label` | registry label; an env row repeats the env key | registry label, or the env key when the row came from the environment |
| `token_env_key` | the env key for an env row; empty for a registry row | not a field on this response |
| `failed` | labels whose listing raised. Never a token | same |

GitHub also returns `cached`, `truncated_env_keys` (an env key, or `credential:<label>` when a registry listing was cut short), and an advisory `push` (GitHub's `permissions.push` for the token's user, not what a fine-grained PAT grants). GitLab returns `cached` and `configured` (`false`, with an empty project list, when the deployment has no GitLab credential — not an error).

## Repository preferences

```bash
# Get per-repo sandbox profile override
curl http://localhost:8200/api/v1/repos/owner/repo/preferences \
  -H "X-Requested-With: XMLHttpRequest"

# Set sandbox profile + language hint for a repo
curl -X PUT http://localhost:8200/api/v1/repos/owner/repo/preferences \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"sandbox_profile": "java-17", "language_hint": "Java", "notes": "Uses Maven"}'
```

The PUT replaces every field it does not mention — except `verification`
(the orchestrator-run test gate and tier path rules, `docs/verification.md`):
omitted or `null` keeps the stored value, `{}` clears it.

## Docker access (admin)

A sandbox profile with `dind_enabled` only *asks* for the shared Docker daemon
(DinD). A sandbox gets the daemon's client certificates and `DOCKER_HOST` when
its repo also holds this grant. **Code from a granted repo can control the
shared Docker daemon, including the proxies holding your tokens**: the daemon
runs every credential proxy and every other job's sandbox, so its certificates
are root on the Docker host. A profile, a stored qa.yaml or a job cannot set it.

| Endpoint | Tier |
|---|---|
| `PUT /api/v1/repos/{repo}/docker-access` | admin |

```bash
# Grant (or {"enabled": false} to revoke). 404 for an unregistered repo.
curl -X PUT http://localhost:8200/api/v1/repos/owner/repo/docker-access \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  -d '{"enabled": true}'
```

`GET /api/v1/repos` and `GET /api/v1/repos/{repo}` return the flag as
`docker_access`. Each change writes a `repo_docker_access_granted` or
`repo_docker_access_revoked` audit row. A sandbox whose profile asks for
Docker on a repo without the grant is refused before it starts, with
`ERR_INCOHERENT_CONFIG` and a message naming this endpoint.

## Sandbox visibility

```bash
# List running sandbox containers (ops)
curl http://localhost:8200/api/v1/sandboxes/active \
  -H "X-Requested-With: XMLHttpRequest"
```

## WebSocket streaming

```javascript
const ws = new WebSocket('ws://localhost:8200/ws/jobs/{job_id}/events');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  // { type: "agent_started", agent: "developer", ... }
  // { type: "progress", message: "Editing src/auth/login.py", ... }
  // { type: "agent_completed", cost_usd: 0.42, ... }
  // Rich event types:
  // { type: "thinking", text: "...", node: "developer" }
  // { type: "tool_call", tool_name: "Edit", input: "main.py", node: "developer" }
  // { type: "tool_result", tool_use_id: "...", content: "...", is_error: false }
  // { type: "text", text: "...", node: "developer" }
  // { type: "cost_update", cost_usd: 1.23, tokens_in: 5000, tokens_out: 2000 }
};
```

## Healing cases

- `GET /api/v1/cases` and `GET /api/v1/cases/{case_id}` — the ledger's read API (#278): one case per finding, from detection to a healed verdict. See `docs/glossary.md`, "Healing case".
  - `?awaiting_decision=true` is "what is waiting for a person": watcher drafts with a ticket that nobody has accepted or rejected — the list the dashboard shows as **Proposals** above the issue list. It is the proposal cap's own rule without the age window, so a draft that aged out of the cap is still listed, and a draft whose ticket was finished or deleted in its tracker is not. `?open_only=true` is a different question (every case not yet `closed`, which includes alerts, findings with no ticket, and merged, deployed and verified cases); `?stage=tracked` is not it either (a person's ticket passes through `tracked`, and a draft whose ticket is gone stays there).
- `POST /api/v1/cases/{case_id}/accept` — accept a watcher finding and dispatch the pipeline on its draft, exactly as adding the trigger label would; refuses anything that is not a `tracked` watcher finding of an enabled watcher stack of this orchestrator (#378). A GitHub draft is dispatched directly. A Linear draft gets the trigger label put on the ticket and is dispatched through the Linear trigger path (#460); a refusal says what happened to the ticket, and a ticket whose Linear project resolves to another repo than the case's is refused untouched. A GitLab draft is still accepted in GitLab (`409`). Body: `{"accepted_by": "<name>"}`. `202` always carries a `job_id`. On the merge-scope allowlist. See `docs/self-watching.md`, "Accepting a finding without the label".
