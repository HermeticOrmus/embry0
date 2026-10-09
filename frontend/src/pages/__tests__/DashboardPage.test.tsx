import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";

// React-query result shape shared by all dashboard hooks.
type FakeQuery<T> = {
  data: T | undefined;
  isPending: boolean;
  isError: boolean;
};

const mockUseStats = vi.fn();
const mockUseQueue = vi.fn();
const mockUseWatcherRuns = vi.fn();

vi.mock("@/hooks/useStats", () => ({
  useStats: () => mockUseStats(),
}));
vi.mock("@/hooks/useQueue", () => ({
  useQueue: () => mockUseQueue(),
}));
vi.mock("@/hooks/useWatcherRuns", () => ({
  useWatcherRuns: () => mockUseWatcherRuns(),
}));

import { DashboardPage } from "../DashboardPage";

function ready<T>(data: T): FakeQuery<T> {
  return { data, isPending: false, isError: false };
}
function loading<T>(): FakeQuery<T> {
  return { data: undefined, isPending: true, isError: false };
}
function errored<T>(): FakeQuery<T> {
  return { data: undefined, isPending: false, isError: true };
}

const ORCH_OK = ready({
  total_issues: 10,
  total_jobs: 20,
  completed: 18,
  failed: 2,
  success_rate: 0.9,
  total_cost_usd: 12.5,
  failure_categories: {},
  daily_cost_usd: 3.45,
  monthly_cost_usd: 47.5,
  queue_depth: 0,
  recent_issues: [
    {
      trace_id: "tr-1",
      issue_number: 42,
      repo: "acme/app",
      tier: "light",
      timestamp: new Date().toISOString(),
      passed: true,
      cost_usd: 1.23,
      status: "completed",
    },
  ],
  token_totals: {
    input_tokens: 100,
    output_tokens: 100,
    cache_read_tokens: 800,
    cache_creation_tokens: 0,
    cache_hit_rate: 0.8,
  },
});

const QUEUE_OK = ready({
  depth: 9,
  pending: 7,
  running: 4,
  awaiting_input: 2,
  paused: 1,
});

const WATCHER_OK = ready([
  {
    id: "wr-1",
    window_start: new Date().toISOString(),
    window_end: new Date().toISOString(),
    log_lines: 120,
    action: "proposed",
    detail: "found a crash loop",
    job_id: null,
    fingerprint: "fp",
    repo: "acme/backend",
    tracker: "linear",
    tracker_key: "RAV-999",
    tracker_url: "https://linear.app/acme/issue/RAV-999",
    created_at: new Date().toISOString(),
  },
]);

function renderPage() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockUseStats.mockReset().mockReturnValue(ORCH_OK);
  mockUseQueue.mockReset().mockReturnValue(QUEUE_OK);
  mockUseWatcherRuns.mockReset().mockReturnValue(WATCHER_OK);
});

describe("DashboardPage", () => {
  it("renders all seven embry0-sourced tiles when the backend is healthy", () => {
    renderPage();

    // Live counts come from /queue — not Companion.
    expect(within(screen.getByTestId("tile-Running")).getByText("4")).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-Queued")).getByText("7")).toBeInTheDocument();
    // Needs You = awaiting_input + paused.
    expect(within(screen.getByTestId("tile-Needs You")).getByText("3")).toBeInTheDocument();

    // Orchestrator-sourced derived metrics.
    expect(within(screen.getByTestId("tile-Job Success Rate")).getByText("90%")).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-Cost Today")).getByText("$3.45")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("tile-Cost This Month")).getByText("$47.50"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("tile-Cache Hit Rate")).getByText("80%"),
    ).toBeInTheDocument();
  });

  it("presents no Companion-sourced data: single orchestrator heartbeat only", () => {
    renderPage();
    const strip = screen.getByTestId("heartbeat-strip");
    expect(within(strip).getByLabelText(/orchestrator/i)).toBeInTheDocument();
    expect(within(strip).queryByLabelText(/agent/i)).toBeNull();
  });

  // Guards the assay finding: "Cost Today" must come from the orchestrator's
  // daily_cost_usd, and the monthly figure must never leak into Today.
  it("sources Cost Today from orchestrator daily_cost_usd", () => {
    renderPage();
    const tile = screen.getByTestId("tile-Cost Today");
    expect(within(tile).getByText("$3.45")).toBeInTheDocument();
    expect(within(tile).queryByText("$47.50")).toBeNull();
  });

  // Guards the assay finding: pass rate must use the orchestrator's existing
  // success_rate field, not be re-derived from completed/failed.
  it("sources Job Success Rate directly from orchestrator success_rate", () => {
    mockUseStats.mockReturnValue(
      ready({
        ...ORCH_OK.data!,
        completed: 5,
        failed: 5,
        total_jobs: 20,
        success_rate: 0.9,
      }),
    );
    renderPage();
    expect(
      within(screen.getByTestId("tile-Job Success Rate")).getByText("90%"),
    ).toBeInTheDocument();
  });

  // #280: job outcome and delivery outcome are separate numbers. A job that
  // failed after opening a PR that merged lifts the second, never the first.
  it("reports job success and delivered work as separate tiles", () => {
    mockUseStats.mockReturnValue(
      ready({
        ...ORCH_OK.data!,
        success_rate: 0.6,
        job_success_rate: 0.4,
        delivery: {
          none: 3,
          pr_open: 2,
          merged: 39,
          closed: 2,
          merged_despite_job_status: 19,
          prs_merged: 34,
          prs_closed: 2,
          prs_open: 2,
          merge_rate: 0.94,
        },
      }),
    );
    renderPage();
    expect(
      within(screen.getByTestId("tile-Job Success Rate")).getByText("40%"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("tile-Succeeded or Merged")).getByText("60%"),
    ).toBeInTheDocument();
    // 39 job rows carry a merged PR, but they are 34 distinct pull requests:
    // the tile says "PRs", so it counts PRs.
    const merged = screen.getByTestId("tile-PRs Merged");
    expect(within(merged).getByText("34")).toBeInTheDocument();
    expect(within(merged).queryByText("39")).toBeNull();
    expect(within(merged).getByText(/human-finished/)).toBeInTheDocument();
  });

  it("falls back to success_rate against a backend that predates the split", () => {
    renderPage();
    expect(
      within(screen.getByTestId("tile-Job Success Rate")).getByText("90%"),
    ).toBeInTheDocument();
    expect(within(screen.getByTestId("tile-PRs Merged")).getByText("0")).toBeInTheDocument();
  });

  it("renders every tile in loading state while queries are pending", () => {
    mockUseStats.mockReturnValue(loading());
    mockUseQueue.mockReturnValue(loading());
    renderPage();
    for (const label of ["Running", "Queued", "Needs You", "Job Success Rate", "Cost Today"]) {
      expect(screen.getByTestId(`tile-${label}`)).toHaveAttribute("data-state", "loading");
    }
  });

  it("scopes an outage to the failing source: queue tiles error, stats tiles stay ready", () => {
    mockUseQueue.mockReturnValue(errored());
    renderPage();
    for (const label of ["Running", "Queued", "Needs You"]) {
      expect(screen.getByTestId(`tile-${label}`)).toHaveAttribute("data-state", "error");
    }
    for (const label of ["Job Success Rate", "Cost Today", "Cost This Month"]) {
      expect(screen.getByTestId(`tile-${label}`)).toHaveAttribute("data-state", "ready");
    }
  });

  it("shows the live-activity band when the queue has activity", () => {
    renderPage();
    // 4 running / 7 queued / 2 awaiting input from QUEUE_OK.
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("awaiting input")).toBeInTheDocument();
  });

  it("renders recent issues and watcher runs panels", () => {
    renderPage();

    const issues = screen.getByTestId("panel-Recent Issues");
    expect(within(issues).getByText("acme/app#42")).toBeInTheDocument();

    const watcher = screen.getByTestId("panel-Watcher");
    expect(within(watcher).getByText("proposed")).toBeInTheDocument();
    expect(within(watcher).getByText("RAV-999")).toBeInTheDocument();
  });

  it("shows honest empty states instead of blank panels", () => {
    mockUseWatcherRuns.mockReturnValue(ready([]));
    renderPage();
    expect(screen.getByText("No watcher ticks recorded yet.")).toBeInTheDocument();
  });
});
