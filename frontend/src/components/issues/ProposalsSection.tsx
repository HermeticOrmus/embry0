import { isAxiosError } from "axios";
import { formatDistanceToNow } from "date-fns";
import { ExternalLink } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/Button";
import { useAcceptProposal, useProposals, useRejectProposal } from "@/hooks/useProposals";
import type { HealingCase } from "@/lib/types";

/**
 * Trackers whose drafts the API accepts (POST /cases/{id}/accept). The others —
 * GitLab, today — are accepted by a label in the tracker itself, so their rows
 * link there instead.
 */
const ACCEPTED_HERE = new Set(["github", "linear"]);

/**
 * Trackers where accepting writes to the ticket: the API puts the trigger label
 * on it before dispatching. The person is told before they confirm.
 */
const LABELLED_ON_ACCEPT = new Set(["linear"]);

const TRACKER_NAMES: Record<string, string> = { linear: "Linear", github: "GitHub", gitlab: "GitLab" };

function trackerName(kind: string | undefined): string {
  return (kind && TRACKER_NAMES[kind]) || kind || "its tracker";
}

function detailOf(error: unknown, fallback: string): string {
  const detail = isAxiosError(error) ? error.response?.data?.detail : undefined;
  return typeof detail === "string" ? detail : fallback;
}

/** When the draft was filed — for a finding that waited for a slot, later than when it was detected. */
function filedAt(proposal: HealingCase): string {
  return proposal.timestamps.tracked_at ?? proposal.timestamps.detected_at;
}

/**
 * Draft tickets the watcher filed that nobody has answered. They are healing
 * cases, not issues — an issue exists only once a draft is accepted — so they
 * sit above the issue list rather than in it. Renders nothing when there are
 * none: the Issues page of a deployment with no watcher is unchanged.
 */
export function ProposalsSection() {
  const proposals = useProposals();
  const accept = useAcceptProposal();
  const reject = useRejectProposal();

  if (proposals.isError) {
    return (
      <div role="alert" className="rounded-lg border border-amber-400/20 px-4 py-3 text-sm text-muted-foreground">
        Could not load the watcher&apos;s proposals.{" "}
        <button className="text-primary hover:underline" onClick={() => proposals.refetch()}>
          Retry
        </button>
      </div>
    );
  }
  const rows = proposals.data?.cases ?? [];
  if (rows.length === 0) return null;

  const handleAccept = (proposal: HealingCase) => {
    const tracker = proposal.tracker;
    const effect =
      tracker && LABELLED_ON_ACCEPT.has(tracker.kind)
        ? `This adds the trigger label to ${tracker.key} in ${trackerName(tracker.kind)} and starts the fix pipeline on ${proposal.repo}.`
        : `This starts the fix pipeline on ${proposal.repo}.`;
    if (!confirm(`Accept "${proposal.title}"? ${effect}`)) return;
    accept.mutate(proposal.id, {
      onSuccess: (result) => toast.success(`Accepted — job ${result.job_id.slice(0, 12)} started`),
      onError: (e) => toast.error(detailOf(e, "Could not accept the proposal")),
    });
  };

  const handleReject = (proposal: HealingCase) => {
    const ticket = proposal.tracker?.key ?? "the ticket";
    const where = trackerName(proposal.tracker?.kind);
    if (
      !confirm(
        `Reject "${proposal.title}"? The watcher stops waiting on it. ${ticket} stays open in ${where} — close it there too.`,
      )
    )
      return;
    reject.mutate(proposal.id, {
      onSuccess: () => toast.success(`Rejected. ${ticket} is still open in ${where}.`),
      onError: (e) => toast.error(detailOf(e, "Could not reject the proposal")),
    });
  };

  const busy = accept.isPending || reject.isPending;

  return (
    <section aria-labelledby="proposals-heading" className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id="proposals-heading" className="text-lg font-semibold text-amber-400">
          Proposals
        </h2>
        <span className="inline-flex items-center rounded-full bg-amber-400/10 px-2.5 py-0.5 text-xs font-medium text-amber-400">
          {proposals.data?.total ?? rows.length}
        </span>
        <p className="text-sm text-muted-foreground">
          Draft tickets the watcher filed. Nothing runs until one is accepted.
        </p>
      </div>

      <div className="overflow-x-auto rounded-lg border border-amber-400/20">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
              <th className="px-4 py-2">Proposal</th>
              <th className="px-4 py-2">Ticket</th>
              <th className="px-4 py-2">Waiting</th>
              <th className="px-4 py-2 text-right">Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((proposal) => {
              const tracker = proposal.tracker;
              const filed = filedAt(proposal);
              return (
                <tr key={proposal.id} className="border-b border-white/[0.04] last:border-0">
                  {/* The one flexible column: it takes what the others leave and
                      truncates, so the decision stays on screen without a
                      sideways scroll. The repo rides under the title. */}
                  <td className="w-full min-w-[14rem] max-w-0 px-4 py-2">
                    <div className="truncate" title={proposal.title}>
                      {proposal.title}
                    </div>
                    <div className="truncate font-mono text-xs text-muted-foreground">{proposal.repo}</div>
                  </td>
                  <td className="whitespace-nowrap px-4 py-2">
                    {tracker?.url ? (
                      <a
                        href={tracker.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-primary hover:underline"
                      >
                        {tracker.key}
                      </a>
                    ) : (
                      // Cases filed before the url was stored (#378).
                      <span>{tracker?.key ?? "—"}</span>
                    )}
                  </td>
                  <td
                    className="whitespace-nowrap px-4 py-2 text-muted-foreground"
                    title={new Date(filed).toLocaleString()}
                  >
                    {formatDistanceToNow(new Date(filed))}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2">
                    <div className="flex items-center justify-end gap-2">
                      {tracker && ACCEPTED_HERE.has(tracker.kind) ? (
                        <Button size="sm" disabled={busy} onClick={() => handleAccept(proposal)}>
                          Accept
                        </Button>
                      ) : tracker?.url ? (
                        <a
                          href={tracker.url}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                        >
                          Accept by label in {trackerName(tracker.kind)}
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      ) : (
                        <span className="text-xs text-muted-foreground">
                          Accept by label in {trackerName(tracker?.kind)}
                        </span>
                      )}
                      <Button size="sm" variant="outline" disabled={busy} onClick={() => handleReject(proposal)}>
                        Reject
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
