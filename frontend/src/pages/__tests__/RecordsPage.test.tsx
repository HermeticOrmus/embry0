import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route, useLocation } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockGet = vi.fn();
vi.mock("@/api/client", () => ({ api: { get: (...a: unknown[]) => mockGet(...a) } }));

vi.mock("@/hooks/useRepos", () => ({
  useRegisteredRepos: () => ({
    data: [
      { repo: "acme/widgets", env_var_count: 0, env_secret_count: 0 },
      { repo: "group/sub/project", env_var_count: 0, env_secret_count: 0 },
    ],
    isLoading: false,
  }),
  defaultRepoAnnotation: (r: { repo: string }) => r.repo,
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { RecordsPage } from "../RecordsPage";

const LIST = {
  repo: "group/sub/project",
  records: [
    {
      slug: "2026-10-01-outage",
      kind: "rca",
      title: "Queue outage",
      current_revision: 2,
      created_at: "2026-10-01T00:00:00Z",
      updated_at: "2026-10-02T00:00:00Z",
      updated_by: "key:ops",
      size_bytes: 120,
    },
    {
      slug: "deploy",
      kind: "runbook",
      title: "Deploy runbook",
      current_revision: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
      updated_by: "key:ops",
      size_bytes: 40,
    },
  ],
};

function detail(revision: number, body: string) {
  return {
    repo: "group/sub/project",
    slug: "2026-10-01-outage",
    kind: "rca",
    title: "Queue outage",
    revision,
    current_revision: 2,
    body,
    author: "key:ops",
    source_job_id: null,
    revision_created_at: "2026-10-02T00:00:00Z",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-02T00:00:00Z",
  };
}

const REVISIONS = {
  repo: "group/sub/project",
  slug: "2026-10-01-outage",
  revisions: [
    { revision: 2, title: "Queue outage", author: "key:ops", source_job_id: null, created_at: "2026-10-02", size_bytes: 120 },
    { revision: 1, title: "Queue outage", author: "key:ops", source_job_id: null, created_at: "2026-10-01", size_bytes: 80 },
  ],
};

let body = "# Queue outage\n\nThe **queue** stalled.\n\n- one\n- two\n";

function route(url: string, config?: { params?: Record<string, unknown> }) {
  if (url.endsWith("/records")) {
    const kind = config?.params?.kind;
    return { data: kind ? { ...LIST, records: LIST.records.filter((r) => r.kind === kind) } : LIST };
  }
  if (url.endsWith("/revisions")) return { data: REVISIONS };
  if (url.includes("/records/")) {
    const rev = (config?.params?.revision as number | undefined) ?? 2;
    return { data: detail(rev, rev === 1 ? "# Old\n\nfirst draft" : body) };
  }
  throw new Error(`unexpected GET ${url}`);
}

let lastSearch = "";
function LocationProbe() {
  lastSearch = useLocation().search;
  return null;
}

function renderPage(entry: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route
            path="/records"
            element={
              <>
                <RecordsPage />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockGet.mockReset();
  mockGet.mockImplementation(async (url: string, config?: { params?: Record<string, unknown> }) => route(url, config));
  body = "# Queue outage\n\nThe **queue** stalled.\n\n- one\n- two\n";
});

describe("RecordsPage", () => {
  it("asks for a repository when none is chosen", () => {
    renderPage("/records");
    expect(screen.getByText(/Choose a repository/)).toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalled();
  });

  it("lists a multi-segment repo's records from its own path", async () => {
    renderPage("/records?repo=group%2Fsub%2Fproject");
    const list = await screen.findByTestId("records-list");
    expect(within(list).getByText("Queue outage")).toBeInTheDocument();
    expect(within(list).getByText("Deploy runbook")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/repos/group/sub/project/records", { params: {} });
    expect(screen.getByTestId("records-repo-link")).toHaveAttribute("href", "/repos/group/sub/project");
  });

  it("filters by kind", async () => {
    renderPage("/records?repo=acme%2Fwidgets");
    await screen.findByTestId("records-list");
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "runbook");
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith("/repos/acme/widgets/records", { params: { kind: "runbook" } }),
    );
    await waitFor(() => expect(screen.queryByText("Queue outage")).not.toBeInTheDocument());
    expect(screen.getByText("Deploy runbook")).toBeInTheDocument();
  });

  it("shows an empty state when the repo has no records", async () => {
    mockGet.mockImplementation(async () => ({ data: { repo: "acme/widgets", records: [] } }));
    renderPage("/records?repo=acme%2Fwidgets");
    expect(await screen.findByTestId("records-empty")).toHaveTextContent("No records yet");
  });

  it("renders the selected record as markdown", async () => {
    renderPage("/records?repo=group%2Fsub%2Fproject");
    await userEvent.click(await screen.findByText("Queue outage"));
    const md = await screen.findByTestId("record-markdown");
    expect(within(md).getByRole("heading", { level: 1, name: "Queue outage" })).toBeInTheDocument();
    expect(within(md).getByText("queue").tagName).toBe("STRONG");
    expect(within(md).getAllByRole("listitem")).toHaveLength(2);
    expect(lastSearch).toContain("slug=2026-10-01-outage");
    expect(mockGet).toHaveBeenCalledWith("/repos/group/sub/project/records/2026-10-01-outage", {
      params: undefined,
    });
  });

  it("requests the picked revision", async () => {
    renderPage("/records?repo=group%2Fsub%2Fproject&slug=2026-10-01-outage");
    await screen.findByTestId("record-markdown");
    await waitFor(() => expect(screen.getAllByRole("option", { name: /rev 1/ })).toHaveLength(1));
    await userEvent.selectOptions(screen.getByLabelText("Revision"), "1");
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith("/repos/group/sub/project/records/2026-10-01-outage", {
        params: { revision: 1 },
      }),
    );
    expect(await screen.findByText("first draft")).toBeInTheDocument();
    expect(lastSearch).toContain("rev=1");
  });

  it("reports a missing record", async () => {
    mockGet.mockImplementation(async (url: string, config?: { params?: Record<string, unknown> }) => {
      if (url.includes("/records/ghost")) throw Object.assign(new Error("404"), { response: { status: 404 } });
      return route(url, config);
    });
    renderPage("/records?repo=acme%2Fwidgets&slug=ghost");
    expect(await screen.findByTestId("record-missing")).toHaveTextContent("ghost");
  });

  it("never renders raw HTML from a record body", async () => {
    body = '# Title\n\n<img src="x" onerror="alert(1)"><script>alert(2)</script>\n\n[bad](javascript:alert(3))';
    const { container } = renderPage("/records?repo=acme%2Fwidgets&slug=2026-10-01-outage");
    await screen.findByTestId("record-markdown");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    const link = screen.getByText("bad").closest("a");
    expect(link?.getAttribute("href") ?? "").not.toMatch(/javascript:/i);
  });
});
