import { api } from "./client";

/** Repo records: per-repository Markdown documents with revision history.
 *  `repo` may contain more than two segments (GitLab nested subgroups), so
 *  it is interpolated as a path, like every other /repos sub-resource. */

export const RECORD_KINDS = ["rca", "audit", "spec", "plan", "spike", "runbook", "note"] as const;
export type RecordKind = (typeof RECORD_KINDS)[number];

export interface RecordSummary {
  slug: string;
  kind: RecordKind;
  title: string;
  current_revision: number;
  created_at: string;
  updated_at: string;
  updated_by: string;
  size_bytes: number;
}

export interface RecordList {
  repo: string;
  records: RecordSummary[];
}

export interface RecordDetail {
  repo: string;
  slug: string;
  kind: RecordKind;
  title: string;
  revision: number;
  current_revision: number;
  body: string;
  author: string;
  source_job_id: string | null;
  revision_created_at: string;
  created_at: string;
  updated_at: string;
}

export interface RecordRevision {
  revision: number;
  title: string;
  author: string;
  source_job_id: string | null;
  created_at: string;
  size_bytes: number;
}

export interface RecordRevisions {
  repo: string;
  slug: string;
  revisions: RecordRevision[];
}

export interface RecordListFilters {
  kind?: string;
  q?: string;
  limit?: number;
}

export async function fetchRecords(repo: string, filters: RecordListFilters = {}): Promise<RecordList> {
  const params: Record<string, string | number> = {};
  if (filters.kind) params.kind = filters.kind;
  if (filters.q) params.q = filters.q;
  if (filters.limit) params.limit = filters.limit;
  const { data } = await api.get<RecordList>(`/repos/${repo}/records`, { params });
  return data;
}

export async function fetchRecord(repo: string, slug: string, revision?: number): Promise<RecordDetail> {
  const { data } = await api.get<RecordDetail>(`/repos/${repo}/records/${encodeURIComponent(slug)}`, {
    params: revision ? { revision } : undefined,
  });
  return data;
}

export async function fetchRecordRevisions(repo: string, slug: string): Promise<RecordRevisions> {
  const { data } = await api.get<RecordRevisions>(
    `/repos/${repo}/records/${encodeURIComponent(slug)}/revisions`,
  );
  return data;
}
