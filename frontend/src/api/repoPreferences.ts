import { api } from "./client";

/** One pre-PR self-check (RAV-1254): run `command` when the diff touches any
 * of `paths` (repo-root-relative prefixes; empty = always). */
export interface DevCheck {
  name: string;
  paths: string[];
  command: string;
}

export interface RepoPreferences {
  repo: string;
  sandbox_profile: string | null;
  language_hint: string | null;
  notes: string;
  execution_mode: string | null;
  auth_mode: string | null;
  git_author_name: string | null;
  git_author_email: string | null;
  dev_checks: DevCheck[];
  base_branch: string | null;
  updated_at: string;
}

export interface RepoPreferencesUpdate {
  sandbox_profile?: string | null;
  language_hint?: string | null;
  notes?: string;
  execution_mode?: string | null;
  auth_mode?: string | null;
  git_author_name?: string | null;
  git_author_email?: string | null;
  dev_checks?: DevCheck[];
  base_branch?: string | null;
}

export async function fetchRepoPreferences(
  fullRepo: string,
): Promise<RepoPreferences | null> {
  const { data } = await api.get<RepoPreferences | null>(`/repos/${fullRepo}/preferences`);
  return data;
}

export async function setRepoPreferences(
  fullRepo: string,
  update: RepoPreferencesUpdate,
): Promise<RepoPreferences> {
  const { data } = await api.put<RepoPreferences>(`/repos/${fullRepo}/preferences`, update);
  return data;
}

export async function deleteRepoPreferences(fullRepo: string): Promise<void> {
  await api.delete(`/repos/${fullRepo}/preferences`);
}
