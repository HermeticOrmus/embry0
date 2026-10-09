import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const mockUseQaAttempts = vi.fn();
const mockUseQaServices = vi.fn();
vi.mock("@/hooks/useQaResults", () => ({
  useQaAttempts: (...a: unknown[]) => mockUseQaAttempts(...a),
  useQaServices: (...a: unknown[]) => mockUseQaServices(...a),
}));
vi.mock("../QAAttemptCard", () => ({
  QAAttemptCard: () => <div data-testid="attempt-card" />,
}));
vi.mock("../QAScreenshotPanel", () => ({
  QAScreenshotPanel: () => <div data-testid="screenshot-panel" />,
}));
vi.mock("../QALiveLogTail", () => ({
  QALiveLogTail: ({ service }: { service: string }) => (
    <div data-testid={`log-tail-${service}`} />
  ),
}));

import { QATab } from "../QATab";

const ATTEMPTS = [{ attempt_n: 1, has_result_json: true, screenshots_count: 2 }];

beforeEach(() => {
  mockUseQaAttempts.mockReset();
  mockUseQaServices.mockReset();
  mockUseQaAttempts.mockReturnValue({ data: ATTEMPTS, isLoading: false });
});

describe("QATab service tabs (RAV-1058 — derived from the live workspace)", () => {
  it("renders one tab per workspace service", () => {
    mockUseQaServices.mockReturnValue({ data: ["backend", "db", "web"] });
    render(<QATab jobId="job-1" jobIsLive={true} />);
    expect(screen.getByRole("tab", { name: "backend" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "db" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "web" })).toBeInTheDocument();
    // First service is the default tab -> its log tail renders.
    expect(screen.getByTestId("log-tail-backend")).toBeInTheDocument();
  });

  it("renders no tabs while the workspace has no services", () => {
    mockUseQaServices.mockReturnValue({ data: [] });
    render(<QATab jobId="job-1" jobIsLive={true} />);
    expect(screen.queryByRole("tab")).toBeNull();
  });

  it("renders no tabs for a finished job", () => {
    mockUseQaServices.mockReturnValue({ data: undefined });
    render(<QATab jobId="job-1" jobIsLive={false} />);
    expect(screen.queryByRole("tab")).toBeNull();
  });
});
