import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";

/**
 * Subscribe to /api/v1/qa/runs/{runId}/events as Server-Sent Events and
 * invalidate the run-detail react-query key on each event so the existing
 * `useQaRun` hook refetches the latest run snapshot. Closes cleanly on
 * `done` events, on transport errors, and on unmount.
 *
 * Auth (RAV-1060): the EventSource browser API cannot send `Authorization`
 * headers, and the master API key must never ride a URL (access logs,
 * history, Referer). The hook first POSTs `/qa/runs/{id}/events-token`
 * (normal Bearer auth via the axios client) for a short-lived token scoped
 * to this single run, and opens the EventSource with that. If the token
 * request or the stream fails, the polling fallback in `useQaRun` (15 s)
 * keeps the dashboard live.
 *
 * Reconnect behavior: browsers auto-reconnect SSE on `onerror` by default.
 * The hook closes the connection on the *first* error to prevent runaway
 * reconnects against a 401 endpoint — polling reconciles state regardless,
 * so a single failure flipping to polling is the correct trade-off.
 */
export function useQaRunEvents(runId: string | undefined): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!runId) return;

    let es: EventSource | null = null;
    let cancelled = false;

    const queryKey = ["qa-dashboard", "runs", runId];

    api
      .post<{ token: string }>(`/qa/runs/${encodeURIComponent(runId)}/events-token`)
      .then(({ data }) => {
        if (cancelled) return;
        const url = `/api/v1/qa/runs/${encodeURIComponent(runId)}/events?token=${encodeURIComponent(data.token)}`;
        // `withCredentials: false` because we'd rather 401 cleanly and fall
        // back to polling than send stale cookies. Cookie-auth deploys can
        // flip this if/when they ship.
        es = new EventSource(url, { withCredentials: false });

        es.onmessage = (ev: MessageEvent) => {
          let event: { type?: string } = {};
          try {
            event = JSON.parse(ev.data) as { type?: string };
          } catch {
            // Malformed event — ignore; the 15s polling fallback will reconcile.
            return;
          }
          // Any event invalidates the run-detail; the hook refetches once.
          queryClient.invalidateQueries({ queryKey });
          if (event.type === "done") {
            es?.close();
          }
        };

        es.onerror = () => {
          // SSE failed (token expiry, network, server restart). Close cleanly
          // so the browser does NOT auto-reconnect — the 15s polling fallback
          // in useQaRun keeps the dashboard live.
          es?.close();
        };
      })
      .catch(() => {
        // Token mint failed (401, network) — polling fallback covers.
      });

    return () => {
      cancelled = true;
      es?.close();
    };
  }, [runId, queryClient]);
}
