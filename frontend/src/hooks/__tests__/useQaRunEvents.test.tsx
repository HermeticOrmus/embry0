import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement, ReactNode } from "react";

// RAV-1060: the hook first POSTs for a run-scoped SSE token, then opens the
// EventSource with it — the master API key never rides the URL.
const mockPost = vi.fn();
vi.mock("@/api/client", () => ({
  api: { post: (...args: unknown[]) => mockPost(...args) },
}));

import { useQaRunEvents } from "../useQaRunEvents";

// ---------------------------------------------------------------------------
// EventSource mock — captures the latest instance so each test can fire
// onmessage / onerror as if the server emitted them.
// ---------------------------------------------------------------------------

interface MockEventSourceInstance {
  url: string;
  withCredentials: boolean;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  onopen: ((event: Event) => void) | null;
  close: ReturnType<typeof vi.fn>;
  readyState: number;
}

let lastEs: MockEventSourceInstance | null = null;
const allEs: MockEventSourceInstance[] = [];

class MockEventSource implements MockEventSourceInstance {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  url: string;
  withCredentials: boolean;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  close = vi.fn(() => {
    this.readyState = MockEventSource.CLOSED;
  });
  readyState: number = MockEventSource.CONNECTING;

  constructor(url: string, options?: { withCredentials?: boolean }) {
    this.url = url;
    this.withCredentials = options?.withCredentials ?? false;
    // Test double: capture the constructed instance so assertions can
    // drive it (onmessage/onerror) and verify close() — aliasing `this`
    // is the whole point of the mock here.
    // eslint-disable-next-line @typescript-eslint/no-this-alias
    lastEs = this;
    allEs.push(this);
  }
}

function makeWrapper(): {
  Wrapper: ({ children }: { children: ReactNode }) => ReactElement;
  qc: QueryClient;
} {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { Wrapper, qc };
}

async function renderAndConnect(runId: string) {
  const { Wrapper, qc } = makeWrapper();
  const utils = renderHook(() => useQaRunEvents(runId), { wrapper: Wrapper });
  await waitFor(() => expect(lastEs).not.toBeNull());
  return { ...utils, qc };
}

describe("useQaRunEvents", () => {
  beforeEach(() => {
    lastEs = null;
    allEs.length = 0;
    mockPost.mockReset();
    mockPost.mockResolvedValue({ data: { token: "v1.9999999999.scoped-token", expires_in: 900 } });
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("does not open an EventSource when runId is undefined", async () => {
    const { Wrapper } = makeWrapper();
    renderHook(() => useQaRunEvents(undefined), { wrapper: Wrapper });
    await act(async () => {});
    expect(mockPost).not.toHaveBeenCalled();
    expect(lastEs).toBeNull();
  });

  it("mints a run-scoped token, then opens the EventSource with it — never the API key", async () => {
    await renderAndConnect("RUN-1");
    expect(mockPost).toHaveBeenCalledWith("/qa/runs/RUN-1/events-token");
    expect(lastEs?.url).toBe("/api/v1/qa/runs/RUN-1/events?token=v1.9999999999.scoped-token");
    expect(lastEs?.withCredentials).toBe(false);
  });

  it("opens no EventSource when the token mint fails (polling fallback covers)", async () => {
    mockPost.mockRejectedValue(new Error("401"));
    const { Wrapper } = makeWrapper();
    renderHook(() => useQaRunEvents("RUN-1b"), { wrapper: Wrapper });
    await act(async () => {});
    expect(lastEs).toBeNull();
  });

  it("opens no EventSource when unmounted before the token resolves", async () => {
    let resolveToken: (v: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise((r) => (resolveToken = r)));
    const { Wrapper } = makeWrapper();
    const { unmount } = renderHook(() => useQaRunEvents("RUN-1c"), { wrapper: Wrapper });
    unmount();
    resolveToken({ data: { token: "t", expires_in: 900 } });
    await act(async () => {});
    expect(lastEs).toBeNull();
  });

  it("invalidates the run-detail query on a subtask_status event", async () => {
    const { qc } = await renderAndConnect("RUN-2");
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const event = new MessageEvent("message", {
      data: JSON.stringify({ type: "subtask_status", app: "hub", status: "passed" }),
    });
    lastEs?.onmessage?.(event);

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["qa-dashboard", "runs", "RUN-2"],
    });
    // subtask_status alone must NOT close the connection — more events to come.
    expect(lastEs?.close).not.toHaveBeenCalled();
  });

  it("invalidates AND closes the connection on a done event", async () => {
    const { qc } = await renderAndConnect("RUN-3");
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    const event = new MessageEvent("message", {
      data: JSON.stringify({ type: "done", run_id: "RUN-3", overall_status: "passed" }),
    });
    lastEs?.onmessage?.(event);

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["qa-dashboard", "runs", "RUN-3"],
    });
    expect(lastEs?.close).toHaveBeenCalledTimes(1);
  });

  it("ignores malformed JSON without throwing", async () => {
    const { qc } = await renderAndConnect("RUN-4");
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");

    expect(() =>
      lastEs?.onmessage?.(new MessageEvent("message", { data: "not-json" })),
    ).not.toThrow();
    expect(invalidateSpy).not.toHaveBeenCalled();
    expect(lastEs?.close).not.toHaveBeenCalled();
  });

  it("closes the connection on transport error so the browser does not auto-reconnect", async () => {
    await renderAndConnect("RUN-5");

    lastEs?.onerror?.(new Event("error"));
    expect(lastEs?.close).toHaveBeenCalledTimes(1);
  });

  it("closes the EventSource on unmount", async () => {
    const { unmount } = await renderAndConnect("RUN-6");

    unmount();
    expect(lastEs?.close).toHaveBeenCalledTimes(1);
  });

  it("recreates the EventSource (with a fresh token) when runId changes", async () => {
    const { Wrapper } = makeWrapper();
    const { rerender } = renderHook(({ id }) => useQaRunEvents(id), {
      wrapper: Wrapper,
      initialProps: { id: "RUN-A" },
    });

    await waitFor(() => expect(allEs.length).toBe(1));
    expect(allEs[0].url).toContain("/api/v1/qa/runs/RUN-A/events?token=");

    rerender({ id: "RUN-B" });
    await waitFor(() => expect(allEs.length).toBe(2));
    expect(allEs[1].url).toContain("/api/v1/qa/runs/RUN-B/events?token=");
    // First connection closed when the effect re-ran.
    expect(allEs[0].close).toHaveBeenCalled();
    expect(mockPost).toHaveBeenCalledTimes(2);
  });
});
