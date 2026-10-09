import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetch = vi.fn();
const mockCreate = vi.fn();
const mockRevoke = vi.fn();
vi.mock("@/api/apiKeys", () => ({
  fetchApiKeys: (...a: unknown[]) => mockFetch(...a),
  createApiKey: (...a: unknown[]) => mockCreate(...a),
  revokeApiKey: (...a: unknown[]) => mockRevoke(...a),
}));

import ApiKeysTab from "../ApiKeysTab";
import { useAuthStore } from "@/stores/authStore";

const KEY = {
  id: "key-1",
  user_id: "usr-1",
  name: "laptop-cli",
  prefix: "emb_abcd1234",
  scope: "operate" as const,
  service: false,
  created_by: "op@example.com",
  created_at: "2026-08-03T12:00:00Z",
  expires_at: null,
  last_used_at: null,
  revoked_at: null,
};

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApiKeysTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockFetch.mockReset();
  mockCreate.mockReset();
  mockRevoke.mockReset();
  useAuthStore.setState({
    status: "authenticated",
    sessionExpired: false,
    authMode: "session",
    user: { email: "op@example.com", name: "Op", tier: "operator" },
    ssoEnabled: true,
    apiKeyLogin: true,
  });
});

describe("ApiKeysTab", () => {
  it("lists keys with scope and status", async () => {
    mockFetch.mockResolvedValue([KEY]);
    renderTab();
    const row = (await screen.findByText("laptop-cli")).closest("tr")!;
    expect(within(row).getByText("operate")).toBeInTheDocument();
    expect(within(row).getByText("active")).toBeInTheDocument();
  });

  it("caps the scope select at the operator tier", async () => {
    mockFetch.mockResolvedValue([]);
    renderTab();
    await screen.findByText(/no api keys/i);
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["read", "operate"]);
  });

  it("offers admin scope and the service checkbox to admins", async () => {
    useAuthStore.setState({ user: { email: "a@example.com", name: "A", tier: "admin" } });
    mockFetch.mockResolvedValue([]);
    renderTab();
    await screen.findByText(/no api keys/i);
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toContain("admin");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toContain("merge");
    expect(screen.getByText("Service key (unowned — for agents/infra such as a chat relay)")).toBeInTheDocument();
  });

  it("shows the minted key exactly once after create", async () => {
    mockFetch.mockResolvedValue([]);
    mockCreate.mockResolvedValue({ ...KEY, key: "emb_SHOWN-ONCE-TOKEN" });
    renderTab();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Name"), "laptop-cli");
    await user.click(screen.getByRole("button", { name: /create key/i }));
    expect(await screen.findByTestId("minted-key")).toHaveTextContent("emb_SHOWN-ONCE-TOKEN");
  });

  it("shows a Claude Code MCP command with the fresh key and this origin", async () => {
    mockFetch.mockResolvedValue([]);
    mockCreate.mockResolvedValue({ ...KEY, key: "emb_SHOWN-ONCE-TOKEN" });
    renderTab();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Name"), "laptop-cli");
    await user.click(screen.getByRole("button", { name: /create key/i }));
    const cmd = await screen.findByTestId("mcp-command");
    expect(cmd).toHaveTextContent("claude mcp add --transport http embry0");
    expect(cmd).toHaveTextContent(`${window.location.origin}/mcp`);
    expect(cmd).toHaveTextContent('--header "Authorization: Bearer emb_SHOWN-ONCE-TOKEN"');
  });

  it("revokes with confirmation", async () => {
    mockFetch.mockResolvedValue([KEY]);
    mockRevoke.mockResolvedValue({ ...KEY, revoked_at: "2026-08-03T13:00:00Z" });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderTab();
    const user = userEvent.setup();
    await user.click(await screen.findByTitle(/revoke this key/i));
    expect(mockRevoke).toHaveBeenCalledWith("key-1");
  });
});
