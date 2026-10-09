import type { BadgeTone } from "@/components/ui/Badge";
import type { WatcherAction } from "@/lib/types";

/** Badge tone per watcher tick action — shared by the Watcher page and the dashboard tile. */
export const WATCHER_ACTION_TONES: Record<WatcherAction, BadgeTone> = {
  proposed: "gold",
  deferred: "warning",
  no_issue: "info",
  skipped_quiet: "neutral",
  skipped_backlog: "neutral",
  skipped_duplicate: "neutral",
  skipped_unparseable: "warning",
  skipped_analysis_cap: "warning",
  error: "error",
};

/** Actions whose id does not read well once the underscores are dropped. */
const ACTION_LABELS: Partial<Record<WatcherAction, string>> = {
  skipped_unparseable: "analysis withheld",
  deferred: "filing deferred",
  skipped_analysis_cap: "analysis budget spent",
};

export function watcherActionLabel(action: WatcherAction): string {
  return ACTION_LABELS[action] ?? action.replaceAll("_", " ");
}

/** Unknown actions (a newer backend) fall back to neutral instead of undefined. */
export function watcherActionTone(action: WatcherAction): BadgeTone {
  return WATCHER_ACTION_TONES[action] ?? "neutral";
}
