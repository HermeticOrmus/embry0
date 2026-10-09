import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockMutate = vi.fn();
const mockToastSuccess = vi.fn();
const mockToastError = vi.fn();

vi.mock("@/hooks/useIssues", () => ({
  useCreateIssue: () => ({ mutate: mockMutate, isPending: false }),
}));
vi.mock("@/hooks/useGitHub", () => ({
  useGitHubRepos: () => ({ data: { repos: [] }, isLoading: false, isError: false }),
}));
vi.mock("sonner", () => ({
  toast: { success: (m: string) => mockToastSuccess(m), error: (m: string) => mockToastError(m) },
}));

import { CreateIssueDialog } from "../CreateIssueDialog";

async function submitWith(response: {
  job_id: string | null;
  dispatch_error_code: string | null;
  dispatch_error: string | null;
}) {
  mockMutate.mockImplementation((_req, opts) => opts.onSuccess(response));
  const onClose = vi.fn();
  render(<CreateIssueDialog onClose={onClose} />);
  await userEvent.type(screen.getByLabelText(/title/i), "Fix the thing");
  await userEvent.click(screen.getByRole("button", { name: /create/i }));
  return onClose;
}

beforeEach(() => vi.clearAllMocks());

describe("CreateIssueDialog", () => {
  it("confirms a created issue whose job started", async () => {
    const onClose = await submitWith({ job_id: "job-1", dispatch_error_code: null, dispatch_error: null });
    expect(mockToastSuccess).toHaveBeenCalledWith("Issue created");
    expect(mockToastError).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("says so when auto-triage could not start a job", async () => {
    const onClose = await submitWith({
      job_id: null,
      dispatch_error_code: "ERR_DISPATCH_FAILED",
      dispatch_error: "The issue was saved but no job was started (boom-free message)",
    });
    expect(mockToastError).toHaveBeenCalledWith(expect.stringContaining("no job was started"));
    expect(mockToastError).toHaveBeenCalledWith(expect.stringContaining("boom-free message"));
    expect(mockToastSuccess).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
