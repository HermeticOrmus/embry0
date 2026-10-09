import { api } from "./client";

export interface GitHubRepo {
  full_name: string;
  description: string | null;
  private: boolean;
  html_url: string;
  default_branch: string;
  language: string | null;
  open_issues_count: number;
}

export interface GitHubRepoListResponse {
  repos: GitHubRepo[];
}

export async function fetchGitHubRepos(): Promise<GitHubRepoListResponse> {
  const { data } = await api.get("/github/repos");
  return data;
}

/** RAV-1301: a token-visible repo not yet in the registry. */
export interface DiscoverableRepo extends GitHubRepo {
  /** Env-key NAME that grants access (e.g. GITHUB_TOKEN__ACME) — never the value. Empty for a registry row. */
  token_env_key: string;
  /** Registry credential that granted this repo. Null for an env token. */
  credential_id: string | null;
  /** Registry label, or the env key when the row came from the environment. */
  credential_label: string;
  /** Advisory: reflects the token's user, not a fine-grained PAT's grant. */
  push: boolean | null;
}

export interface DiscoverableReposResponse {
  repos: DiscoverableRepo[];
  truncated_env_keys: string[];
  cached: boolean;
  /** Labels of credentials whose listing raised. Never a token. */
  failed: string[];
}

export async function fetchDiscoverableRepos(refresh = false): Promise<DiscoverableReposResponse> {
  // Cold fan-out = up to 5 pages x N tokens against GitHub; give it headroom
  // beyond the 30s client default (bootstrap.ts precedent).
  const { data } = await api.get("/github/discoverable-repos", {
    params: refresh ? { refresh: true } : undefined,
    timeout: 60_000,
  });
  return data;
}
