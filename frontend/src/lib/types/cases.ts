/** Healing cases — rows from GET /cases (the fields the dashboard reads). */

export interface CaseTracker {
  /** "linear" | "github" | "gitlab". */
  kind: string;
  /** Tracker-native key: Linear "ENG-123", a GitHub issue number. */
  key: string;
  /** Null on cases filed before the url was stored (#378). */
  url: string | null;
}

export interface CaseTimestamps {
  detected_at: string;
  /** When the draft ticket was filed; later than detected_at for a finding that waited for a draft slot. */
  tracked_at: string | null;
}

export interface HealingCase {
  id: string;
  repo: string;
  title: string;
  origin: string;
  stage: string;
  severity: string | null;
  tracker: CaseTracker | null;
  timestamps: CaseTimestamps;
}

export interface CaseListResponse {
  cases: HealingCase[];
  total: number;
  limit: number;
  next_cursor: string | null;
}

/** POST /cases/{id}/accept — the fix pipeline it started. */
export interface CaseAcceptResponse {
  case_id: string;
  issue_id: string;
  job_id: string;
}
