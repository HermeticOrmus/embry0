/** RAV-657 log-watcher audit trail — rows from GET /watcher/runs. */

export type WatcherAction =
  | "skipped_quiet"
  | "skipped_backlog"
  | "skipped_duplicate"
  /** Analysis withheld: the last attempt on this window was unparseable and nothing changed since. */
  | "skipped_unparseable"
  | "no_issue"
  | "proposed"
  /** A proposal made while the draft cap was full: recorded, filed when a slot frees (#283). */
  | "deferred"
  /** The draft cap was full and the day's budget for full-cap analyses was spent (#283). */
  | "skipped_analysis_cap"
  | "error";

export interface WatcherRun {
  id: string;
  window_start: string;
  window_end: string;
  log_lines: number;
  action: WatcherAction;
  detail: string | null;
  job_id: string | null;
  fingerprint: string | null;
  /** The stack this tick watched ("owner/name"); "" for ticks before stacks existed. */
  repo: string;
  /** The filed ticket, tracker-neutral since #378 (was linear_issue_identifier). */
  tracker: string | null;
  tracker_key: string | null;
  tracker_url: string | null;
  created_at: string;
}

/** POST /watcher/run — the tick's summary (fields vary by action). */
export interface WatcherTickResult {
  action: WatcherAction;
  log_lines?: number;
  job_id?: string | null;
  fingerprint?: string | null;
  tracker?: string | null;
  tracker_key?: string | null;
  tracker_url?: string | null;
}
