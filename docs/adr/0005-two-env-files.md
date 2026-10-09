# ADR 0005: Two `.env` files — the repo-root one is the container runtime env

Status: accepted (2026-07-18)

Secrets and tokens live in the **repo-root `.env`**: it is what `env_file: ../.env` loads into the orchestrator container, so `GITHUB_TOKEN`, `GITHUB_TOKEN__<OWNER>`, `CLAUDE_CODE_OAUTH_TOKEN`, `PROXY_ADMIN_TOKEN`, `API_KEY`, `IMAGE_REGISTRY` and the tunnel tokens are edited there. `infra/.env` exists only to feed docker-compose `${…}` interpolation (`POSTGRES_*`, `PROD_PORT`, resource caps) and is loaded into no container; its historical copies of the secrets are dead weight. Whenever a doc says "`.env`" for a secret, it means the root file.

The split is visible at [`infra/docker-compose.yml`](../../infra/docker-compose.yml) (`env_file: ../.env` on the orchestrator). It predates the decision — the rule was written down in `bccf601` (2026-07-18, "docs: clarify which .env file containers actually read") after the ambiguity cost debugging time; `f49a5e1` (2026-04-28) had already stopped leaking the full `.env` into the cloudflared container.
