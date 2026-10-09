import { api } from "./client";

export interface BootstrapRequest {
  repo: string;
  app?: string;
  boot_command?: string;
  frontend_url?: string;
  ready_check_url?: string;
  sandbox_profile?: string;
  force?: boolean;
}

export interface BootstrapResult {
  repo: string;
  default_branch: string | null;
  private: boolean | null;
  qa_config_path: string;
  qa_required: string; // "auto" | "never"
  app: string | null;
  sandbox_profile: string | null;
  steps: string[];
}

/**
 * One-command repo bootstrap: verifies the GitHub repo, registers a starter
 * qa.yaml v2 in the external store, and optionally seeds repo preferences.
 *
 * Synchronous on the backend (live GitHub call inside) — the per-request
 * timeout is widened past the client's 30s global. The backend request model
 * is extra="forbid" and slug-validates fields, so empty optionals must be
 * omitted entirely, never sent as "".
 */
export async function bootstrapRepo(req: BootstrapRequest): Promise<BootstrapResult> {
  const payload: BootstrapRequest = { repo: req.repo };
  if (req.app) payload.app = req.app;
  if (req.boot_command) payload.boot_command = req.boot_command;
  if (req.frontend_url) payload.frontend_url = req.frontend_url;
  if (req.ready_check_url) payload.ready_check_url = req.ready_check_url;
  if (req.sandbox_profile) payload.sandbox_profile = req.sandbox_profile;
  if (req.force) payload.force = true;

  const { data } = await api.post<BootstrapResult>("/repos/bootstrap", payload, {
    timeout: 60_000,
    suppressValidationToast: true,
  });
  return data;
}
