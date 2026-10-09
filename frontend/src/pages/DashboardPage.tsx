import type { ReactNode } from "react";
import { Link } from "react-router";
import { useStats } from "@/hooks/useStats";
import { useQueue } from "@/hooks/useQueue";
import { useWatcherRuns } from "@/hooks/useWatcherRuns";
import { LiveActivityBand } from "@/components/dashboard/LiveActivityBand";
import { Badge } from "@/components/ui/Badge";
import { formatDate } from "@/lib/utils";
import { watcherActionLabel, watcherActionTone } from "@/lib/watcherActions";
import {
  Heartbeat,
  SingleSourceTile,
  type SingleSourceQuery,
} from "@/components/vitals";

// Format helpers — kept local; they're the only translation layer between
// raw backend numbers and tile strings.
const formatCount = (n: number) => String(n);
const formatPercent = (n: number) => `${Math.round(n * 100)}%`;
const formatUsd = (n: number) => `$${n.toFixed(2)}`;

// Project a useQuery result onto the SingleSourceQuery contract with a
// per-tile value selector. Keeping the selector at the call site means
// each tile names its own source field, which makes the assay's
// source-mapping guards trivially auditable.
function selectQuery<TSource, TValue>(
  result: { data: TSource | undefined; isPending: boolean; isError: boolean },
  select: (source: TSource) => TValue,
): SingleSourceQuery<TValue> {
  return {
    isError: result.isError,
    isPending: result.isPending,
    data: result.data === undefined ? undefined : select(result.data),
  };
}

/**
 * Dashboard (rebuilt in RAV-1058 increment 1) — every number on this page
 * comes from embry0's own backend. Companion (`/agent`) stats used to fill
 * 4 of 7 tiles here, presenting another product's task counts as embry0's;
 * live counts now come from /queue and the rest from /stats, plus the
 * watcher audit trail shipped in RAV-657.
 */
export function DashboardPage() {
  const stats = useStats();
  const queue = useQueue();
  const watcher = useWatcherRuns(5);

  const recentIssues = stats.data?.recent_issues.slice(0, 6) ?? [];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5 xl:grid-cols-9">
        <SingleSourceTile
          label="Running"
          query={selectQuery(queue, (q) => q.running)}
          format={formatCount}
        />
        <SingleSourceTile
          label="Queued"
          query={selectQuery(queue, (q) => q.pending)}
          format={formatCount}
        />
        <SingleSourceTile
          label="Needs You"
          query={selectQuery(queue, (q) => q.awaiting_input + q.paused)}
          format={formatCount}
        />
        {/* #280: two outcomes, never folded into one number. How the job
            itself ended, and whether its work was delivered — a job that
            failed a late stage after opening a PR that merged is the second
            without being the first. */}
        <SingleSourceTile
          label="Job Success Rate"
          query={selectQuery(stats, (s) => s.job_success_rate ?? s.success_rate)}
          format={formatPercent}
        />
        <SingleSourceTile
          label="Succeeded or Merged"
          query={selectQuery(stats, (s) => s.success_rate)}
          format={formatPercent}
        />
        {/* Distinct pull requests, not job rows — several jobs share one PR.
            The trend line says whose work it was: some of these PRs were
            finished by a person from an embry0 branch. */}
        <SingleSourceTile
          label="PRs Merged"
          query={selectQuery(stats, (s) => s.delivery?.prs_merged ?? 0)}
          format={formatCount}
          trend="embry0 branches, incl. human-finished"
        />
        <SingleSourceTile
          label="Cost Today"
          query={selectQuery(stats, (s) => s.daily_cost_usd)}
          format={formatUsd}
        />
        <SingleSourceTile
          label="Cost This Month"
          query={selectQuery(stats, (s) => s.monthly_cost_usd)}
          format={formatUsd}
        />
        <SingleSourceTile
          label="Cache Hit Rate"
          query={selectQuery(stats, (s) => s.token_totals?.cache_hit_rate ?? 0)}
          format={formatPercent}
        />
      </div>

      {queue.data && (
        <LiveActivityBand
          running={queue.data.running}
          queued={queue.data.pending}
          awaitingInput={queue.data.awaiting_input}
        />
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <DashboardPanel
          title="Recent Issues"
          viewAll={{ to: "/issues", label: "All issues" }}
          isPending={stats.isPending}
          isError={stats.isError}
          isEmpty={recentIssues.length === 0}
          emptyText="No issues processed yet."
        >
          <ul className="divide-y divide-white/[0.06]">
            {recentIssues.map((issue) => (
              <li key={issue.trace_id} className="flex items-center gap-3 py-2">
                <Badge tone={issue.passed ? "success" : "error"}>
                  {issue.status ?? (issue.passed ? "passed" : "failed")}
                </Badge>
                <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                  {issue.repo}#{issue.issue_number}
                </span>
                {issue.cost_usd !== undefined && (
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {formatUsd(issue.cost_usd)}
                  </span>
                )}
                <span className="shrink-0 text-xs text-muted-foreground">
                  {formatDate(issue.timestamp)}
                </span>
              </li>
            ))}
          </ul>
        </DashboardPanel>

        <DashboardPanel
          title="Watcher"
          viewAll={{ to: "/watcher", label: "All ticks" }}
          isPending={watcher.isPending}
          isError={watcher.isError}
          isEmpty={(watcher.data?.length ?? 0) === 0}
          emptyText="No watcher ticks recorded yet."
        >
          <ul className="divide-y divide-white/[0.06]">
            {watcher.data?.map((run) => (
              <li key={run.id} className="flex items-center gap-3 py-2">
                <Badge tone={watcherActionTone(run.action)}>
                  {watcherActionLabel(run.action)}
                </Badge>
                <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">
                  {run.tracker_key ?? `${run.log_lines} log lines`}
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {formatDate(run.created_at)}
                </span>
              </li>
            ))}
          </ul>
        </DashboardPanel>
      </div>

      <div
        data-testid="heartbeat-strip"
        className="flex flex-wrap items-center gap-6"
      >
        <Heartbeat label="orchestrator live" />
      </div>
    </div>
  );
}

interface DashboardPanelProps {
  title: string;
  viewAll?: { to: string; label: string };
  isPending: boolean;
  isError: boolean;
  isEmpty: boolean;
  emptyText: string;
  children: ReactNode;
}

function DashboardPanel({
  title,
  viewAll,
  isPending,
  isError,
  isEmpty,
  emptyText,
  children,
}: DashboardPanelProps) {
  let body: ReactNode;
  if (isError) {
    body = <p className="py-2 text-sm text-muted-foreground">Unavailable</p>;
  } else if (isPending) {
    body = <p className="py-2 text-sm text-muted-foreground">Loading…</p>;
  } else if (isEmpty) {
    body = <p className="py-2 text-sm text-muted-foreground">{emptyText}</p>;
  } else {
    body = children;
  }

  return (
    <section
      data-testid={`panel-${title}`}
      className="rounded-lg border border-border bg-card px-4 py-3"
    >
      <header className="flex items-center justify-between">
        <h2 className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          {title}
        </h2>
        {viewAll && (
          <Link
            to={viewAll.to}
            className="text-xs text-primary hover:underline"
          >
            {viewAll.label} →
          </Link>
        )}
      </header>
      <div className="mt-1">{body}</div>
    </section>
  );
}
