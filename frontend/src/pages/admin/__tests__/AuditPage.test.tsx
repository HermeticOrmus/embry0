import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetchAuditLog = vi.fn();
vi.mock("@/api/audit", () => ({
  fetchAuditLog: (...a: unknown[]) => mockFetchAuditLog(...a),
}));

import { AuditPage } from "../AuditPage";

const ENTRY = {
  id: 1,
  action: "user.login",
  actor: "t@example.com",
  details: { tier: "admin" },
  issue_id: null,
  created_at: "2026-08-01T12:00:00Z",
};

const ISSUE_ENTRY = {
  id: 2,
  action: "issue.created",
  actor: "op@example.com",
  details: {},
  issue_id: "iss-abc123def456",
  created_at: "2026-08-01T13:00:00Z",
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuditPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockFetchAuditLog.mockReset();
});

describe("AuditPage", () => {
  it("renders labeled events with actor and detail", async () => {
    mockFetchAuditLog.mockResolvedValue({ entries: [ENTRY], total: 1 });
    renderPage();
    expect(await screen.findByText("Signed in")).toBeInTheDocument();
    expect(screen.getByText("t@example.com")).toBeInTheDocument();
    expect(screen.getByText("tier admin")).toBeInTheDocument();
  });

  it("links issue-scoped rows to the issue page", async () => {
    mockFetchAuditLog.mockResolvedValue({ entries: [ISSUE_ENTRY], total: 1 });
    renderPage();
    const link = await screen.findByRole("link", { name: /iss-abc123de/ });
    expect(link).toHaveAttribute("href", "/issues/iss-abc123def456");
  });

  it("shows the empty state when no rows match", async () => {
    mockFetchAuditLog.mockResolvedValue({ entries: [], total: 0 });
    renderPage();
    expect(await screen.findByText(/no audit events/i)).toBeInTheDocument();
  });

  it("passes the action filter and resets paging", async () => {
    mockFetchAuditLog.mockResolvedValue({ entries: [ENTRY], total: 1 });
    renderPage();
    await screen.findByText("Signed in");
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Actions"), "apikey.created");
    await vi.waitFor(() => {
      const lastCall = mockFetchAuditLog.mock.calls.at(-1)?.[0] as Record<string, unknown>;
      expect(lastCall.action).toBe("apikey.created");
      expect(lastCall.offset).toBe(0);
    });
  });

  it("debounces the actor filter", async () => {
    mockFetchAuditLog.mockResolvedValue({ entries: [ENTRY], total: 1 });
    renderPage();
    await screen.findByText("Signed in");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Actor"), "hermes");
    await vi.waitFor(() => {
      const lastCall = mockFetchAuditLog.mock.calls.at(-1)?.[0] as Record<string, unknown>;
      expect(lastCall.actor).toBe("hermes");
    });
  });
});
