import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetchRepos = vi.fn();
const mockFetchGlobalContext = vi.fn();
const mockFetchRepoContexts = vi.fn();
const mockUpdateRepoContext = vi.fn();

vi.mock("@/api/repos", () => ({
  fetchRepos: () => mockFetchRepos(),
}));

vi.mock("@/api/config", () => ({
  fetchGlobalContext: () => mockFetchGlobalContext(),
  updateGlobalContext: vi.fn(),
  fetchRepoContexts: () => mockFetchRepoContexts(),
  updateRepoContext: (repo: string, ctx: unknown) => mockUpdateRepoContext(repo, ctx),
  deleteRepoContext: vi.fn(),
}));

import ContextTab from "../ContextTab";

const REGISTRY = [
  {
    repo: "acme/backend",
    active: true,
    notes: "",
    created_at: "",
    updated_at: "",
    forge: "github" as const,
    has_qa_config: true,
    env_var_count: 2,
    env_secret_count: 2,
  },
  {
    repo: "acme/console",
    active: true,
    notes: "",
    created_at: "",
    updated_at: "",
    forge: "github" as const,
    has_qa_config: false,
    env_var_count: 22,
    env_secret_count: 20,
  },
];

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ContextTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFetchRepos.mockResolvedValue(REGISTRY);
  mockFetchGlobalContext.mockResolvedValue({ system_context: "", assistant_context: "" });
  mockFetchRepoContexts.mockResolvedValue([
    {
      repo: "acme/backend",
      system_context: "quote rules",
      assistant_context: "",
    },
  ]);
});

describe("ContextTab — RAV-1296 repo selector", () => {
  it("annotates configured repos and lands on the first one", async () => {
    renderTab();
    const picker = await screen.findByRole("combobox", { name: "Repository" });
    await waitFor(() => expect(picker).toHaveValue("acme/backend"));
    expect(
      within(picker).getByRole("option", { name: "acme/backend — context set" }),
    ).toBeInTheDocument();
    expect(
      await within(picker).findByRole("option", { name: "acme/console" }),
    ).toBeInTheDocument();
    // The selected repo's context is loaded into the single editor.
    expect(screen.getByDisplayValue("quote rules")).toBeInTheDocument();
  });

  it("shows one editor at a time, not a stacked card per repo", async () => {
    renderTab();
    await screen.findByDisplayValue("quote rules");
    // Exactly one per-repo editor is rendered (the old view stacked one
    // card per configured repo).
    expect(
      screen.getAllByPlaceholderText("Repository-specific system context..."),
    ).toHaveLength(1);
  });

  it("selecting an unconfigured repo opens an empty draft whose Save creates it", async () => {
    mockUpdateRepoContext.mockResolvedValue({});
    renderTab();
    const picker = await screen.findByRole("combobox", { name: "Repository" });
    await waitFor(() => expect(picker).toHaveValue("acme/backend"));
    // The registry loads apart from the contexts: until it does, the picker
    // is disabled and lists only the configured repos.
    await within(picker).findByRole("option", { name: "acme/console" });
    await waitFor(() => expect(picker).toBeEnabled());
    await userEvent.selectOptions(picker, "acme/console");
    // The single repo editor now shows the newly-selected repo as a draft.
    const system = screen.getByPlaceholderText("Repository-specific system context...");
    expect(system).toHaveValue("");
    await userEvent.type(system, "cc context");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mockUpdateRepoContext).toHaveBeenCalledWith("acme/console", {
        system_context: "cc context",
        assistant_context: "",
      }),
    );
  });
});
