# ADR 0006: Credentials live in an encrypted registry, resolved through one service

Status: accepted (2026-09-29)

Every account embry0 acts as — forge tokens now, LLM and SSH credentials in later slices — is a row in `credentials`, encrypted with the same Fernet provider as repo env-var secrets, and reaches runtime code only through `CredentialResolver` (repo > owner > host > global > env). Env variables such as `GITHUB_TOKEN__<OWNER>` are an import source and the resolver's last step, not a place new code may read from. Reading `os.environ` for a credential anywhere else is therefore forbidden.

Evidence: the 2026-09-29 exploration found `CLAUDE_CODE_OAUTH_TOKEN` read in five places and `ANTHROPIC_API_KEY` in four, one enrollment payload sent to every proxy regardless of forge, and no record of which token a job used. First landing: migration 86.
