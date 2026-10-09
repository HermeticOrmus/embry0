import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

const mockMutate = vi.fn();
vi.mock("@/hooks/useJobs", () => ({
  useCreateJob: () => ({ mutate: mockMutate, isPending: false }),
}));
vi.mock("@/hooks/useGitHub", () => ({
  useGitHubRepos: () => ({
    data: { repos: [{ full_name: "acme/widgets" }] },
    isLoading: false,
    isError: false,
  }),
}));
vi.mock("@/hooks/usePipelines", () => ({
  useTemplates: () => ({ data: [{ template_id: "t1", name: "quick-fix", description: "", created_at: "" }] }),
}));
vi.mock("@/hooks/useSandboxProfiles", () => ({ useSandboxProfiles: () => ({ data: [] }) }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/hooks/useModels", () => ({ useModels: () => ({ models: [], isLoading: false }) }));

import { NewJobForm } from "../NewJobForm";

beforeEach(() => {
  vi.clearAllMocks();
});

function renderForm() {
  return render(
    <MemoryRouter>
      <NewJobForm onClose={vi.fn()} />
    </MemoryRouter>,
  );
}

describe("NewJobForm", () => {
  it("dispatches a minimal repo+task payload when advanced is untouched", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.selectOptions(screen.getByLabelText("Repository"), "acme/widgets");
    await user.type(screen.getByLabelText("Task"), "Ship it");
    await user.click(screen.getByRole("button", { name: "Dispatch" }));

    expect(mockMutate).toHaveBeenCalledWith(
      { repo: "acme/widgets", task: "Ship it" },
      expect.anything(),
    );
  });

  it("folds advanced options into the dispatch payload", async () => {
    const user = userEvent.setup();
    renderForm();
    await user.selectOptions(screen.getByLabelText("Repository"), "acme/widgets");
    await user.type(screen.getByLabelText("Task"), "Ship it");
    await user.click(screen.getByLabelText("Advanced options"));
    await user.selectOptions(screen.getByLabelText("Pipeline Template"), "quick-fix");
    await user.click(screen.getByRole("button", { name: "Dispatch" }));

    expect(mockMutate).toHaveBeenCalledWith(
      { repo: "acme/widgets", task: "Ship it", pipeline_template: "quick-fix" },
      expect.anything(),
    );
  });
});
