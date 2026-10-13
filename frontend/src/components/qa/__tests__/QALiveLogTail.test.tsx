import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";

// #634: EventSource cannot send an Authorization header, so the tail first
// POSTs (with normal auth) for a job-scoped token and opens the stream with
// that — never with the API key, and never with no credential at all.
const mockPost = vi.fn();
vi.mock("@/api/client", () => ({
  api: { post: (...args: unknown[]) => mockPost(...args) },
}));

import { QALiveLogTail } from "../QALiveLogTail";

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  mockPost.mockReset();
  vi.stubGlobal("EventSource", MockEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("QALiveLogTail", () => {
  it("opens the stream with a job-scoped token minted through the authenticated client", async () => {
    mockPost.mockResolvedValue({ data: { token: "v1.123.abc", expires_in: 900 } });

    render(<QALiveLogTail jobId="job-1" service="web app" active />);

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    expect(mockPost, "the token must be minted with the API client, which carries the key").toHaveBeenCalledWith(
      "/jobs/job-1/qa/log-stream-token",
    );
    expect(MockEventSource.instances[0].url, "the EventSource URL must carry the scoped token").toBe(
      "/api/v1/jobs/job-1/artifacts/logs/web%20app?follow=true&token=v1.123.abc",
    );
  });

  it("shows the stream closed and opens nothing when the token cannot be minted", async () => {
    mockPost.mockRejectedValue(new Error("401"));

    render(<QALiveLogTail jobId="job-1" service="web" active />);

    await screen.findByText(/stream closed/i);
    expect(MockEventSource.instances, "no unauthenticated stream may be opened").toHaveLength(0);
  });

  it("appends streamed lines and closes the stream on unmount", async () => {
    mockPost.mockResolvedValue({ data: { token: "t", expires_in: 900 } });

    const { unmount } = render(<QALiveLogTail jobId="job-1" service="web" active />);
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const es = MockEventSource.instances[0];

    act(() => {
      es.onopen?.();
      es.onmessage?.({ data: "server listening" } as MessageEvent);
    });
    expect(await screen.findByText("server listening")).toBeTruthy();

    unmount();
    expect(es.close).toHaveBeenCalled();
  });

  it("does not open a stream after unmounting while the token is in flight", async () => {
    let resolve: (v: unknown) => void = () => {};
    mockPost.mockReturnValue(new Promise((r) => (resolve = r)));

    const { unmount } = render(<QALiveLogTail jobId="job-1" service="web" active />);
    unmount();
    await act(async () => resolve({ data: { token: "late", expires_in: 900 } }));

    expect(MockEventSource.instances, "a late token must not open an orphaned stream").toHaveLength(0);
  });

  it("does not mint or stream for a job that is no longer live", () => {
    render(<QALiveLogTail jobId="job-1" service="web" active={false} />);
    expect(mockPost).not.toHaveBeenCalled();
    expect(MockEventSource.instances).toHaveLength(0);
  });
});
