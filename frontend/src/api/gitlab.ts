import { api } from "./client";

/** Project from GET /gitlab/discoverable-projects (#169). */
export interface GitLabProject {
  full_name: string;
  description: string | null;
  private: boolean;
  html_url: string;
  default_branch: string;
  open_issues_count: number;
  /** Registry credential that granted this project. Null for an env token. */
  credential_id: string | null;
  /** Registry label, or the env key when the row came from the environment. */
  credential_label: string;
}

export interface DiscoverableProjectsResponse {
  projects: GitLabProject[];
  cached: boolean;
  /** False when no GITLAB_TOKEN is configured — a GitHub-only deployment. */
  configured: boolean;
  /** Labels of credentials whose listing raised. Never a token. */
  failed: string[];
}

export async function fetchDiscoverableProjects(refresh = false): Promise<DiscoverableProjectsResponse> {
  const { data } = await api.get<DiscoverableProjectsResponse>("/gitlab/discoverable-projects", {
    params: refresh ? { refresh: true } : undefined,
  });
  return data;
}
