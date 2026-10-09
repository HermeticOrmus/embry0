import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError } from "axios";

const mockQueryLogs = vi.fn();
const mockFetchLogContainers = vi.fn();
vi.mock("@/api/logs", () => ({
  queryLogs: (params: unknown) => mockQueryLogs(params),
  fetchLogContainers: () => mockFetchLogContainers(),
}));

import { LogsPage } from "../LogsPage";

const RESPONSE = {
  query: '{container_name=~".+"}',
  window_start: new Date().toISOString(),
  window_end: new Date().toISOString(),
  count: 2,
  lines: [
    { ts: new Date().toISOString(), container: "embry0-orchestrator", line: "job started" },
    { ts: new Date().toISOString(), container: "embry0-orchestrator", line: "job completed" },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LogsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFetchLogContainers.mockResolvedValue({
    containers: ["embry0-frontend", "embry0-orchestrator"],
    cached: false,
  });
});

describe("LogsPage — RAV-1295", () => {
  it("runs the all-containers query on mount (no empty landing state)", async () => {
    mockQueryLogs.mockResolvedValue(RESPONSE);
    renderPage();
    await waitFor(() =>
      expect(mockQueryLogs).toHaveBeenCalledWith({
        containers: undefined,
        search: undefined,
        since_minutes: 60,
        limit: 500,
      }),
    );
    expect(await screen.findByTestId("logs-tail")).toBeInTheDocument();
    expect(screen.getByText("job started")).toBeInTheDocument();
  });

  it("multi-selecting containers sends a comma-separated exact list", async () => {
    mockQueryLogs.mockResolvedValue(RESPONSE);
    renderPage();
    const user = userEvent.setup();
    await waitFor(() => expect(mockQueryLogs).toHaveBeenCalled());
    mockQueryLogs.mockClear();

    await user.click(screen.getByRole("button", { name: "All containers" }));
    await user.click(await screen.findByRole("checkbox", { name: "embry0-orchestrator" }));
    await user.click(screen.getByRole("checkbox", { name: "embry0-frontend" }));
    await user.type(screen.getByLabelText(/line filter/i), "job");
    await user.selectOptions(screen.getByLabelText("Window"), "360");
    await user.click(screen.getByRole("button", { name: /query/i }));

    await waitFor(() =>
      expect(mockQueryLogs).toHaveBeenCalledWith({
        containers: "embry0-orchestrator,embry0-frontend",
        search: "job",
        since_minutes: 360,
        limit: 500,
      }),
    );
  });

  it("live tail registers a 5s poll and tears it down on toggle-off", async () => {
    mockQueryLogs.mockResolvedValue(RESPONSE);
    const setSpy = vi.spyOn(window, "setInterval");
    const clearSpy = vi.spyOn(window, "clearInterval");
    renderPage();
    const user = userEvent.setup();
    await waitFor(() => expect(mockQueryLogs).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Live tail" }));
    await waitFor(() =>
      expect(setSpy).toHaveBeenCalledWith(expect.any(Function), 5000),
    );
    // The registered callback re-runs the query — the poll IS the tail.
    const tick = setSpy.mock.calls.find((c) => c[1] === 5000)![0] as () => void;
    tick();
    await waitFor(() => expect(mockQueryLogs).toHaveBeenCalledTimes(2));

    clearSpy.mockClear();
    await user.click(screen.getByRole("button", { name: "Tail on" }));
    await waitFor(() => expect(clearSpy).toHaveBeenCalled());
  });

  it("explains a 503 as Loki-not-configured", async () => {
    mockQueryLogs.mockRejectedValue(
      new AxiosError("503", "503", undefined, undefined, {
        data: { detail: "Loki is not configured" },
        status: 503,
        statusText: "",
        headers: {},
        config: {} as never,
      }),
    );
    renderPage();
    expect(
      await screen.findByText(/Loki is not configured on the orchestrator/),
    ).toBeInTheDocument();
  });

  it("shows an empty state for a query with no matches", async () => {
    mockQueryLogs.mockResolvedValue({ ...RESPONSE, count: 0, lines: [] });
    renderPage();
    expect(await screen.findByText("No matching log lines")).toBeInTheDocument();
  });
});
