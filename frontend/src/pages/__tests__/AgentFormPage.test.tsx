import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";

const mockUseAgent = vi.fn();
const mockUpdateMutate = vi.fn();
const mockCreateMutate = vi.fn();

vi.mock("@/hooks/useAgents", () => ({
  useAgent: (type: string | null) => mockUseAgent(type),
  useCreateAgent: () => ({ mutate: mockCreateMutate, isPending: false }),
  useUpdateAgent: () => ({ mutate: mockUpdateMutate, isPending: false }),
}));
vi.mock("@/hooks/useModels", () => {
  // Referentially stable — a fresh array per call re-fires AgentFormPage's
  // populate effect forever (the same loop the useModels memo guards against).
  const MODELS = { models: ["claude-opus-5", "claude-sonnet-5"], isLoading: false };
  return { useModels: () => MODELS };
});

import { AgentFormPage } from "../AgentFormPage";

const EXISTING = {
  type: "developer",
  description: "Writes code",
  model: "claude-opus-5",
  tools: ["Read", "Edit"],
  skills: [],
  system_prompt: "You write code.",
  execution_mode: "cli",
  auth_mode: "oauth",
  mcp_servers: { playwright: { type: "stdio", command: "npx" } },
  is_builtin: true,
};

function renderEdit(type = "developer") {
  return render(
    <MemoryRouter initialEntries={[`/agents/${type}`]}>
      <Routes>
        <Route path="/agents/:type" element={<AgentFormPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAgent.mockReturnValue({ data: EXISTING, isLoading: false });
});

describe("AgentFormPage — execution depth (RAV-1058)", () => {
  it("populates execution mode, auth mode, and mcp servers from the definition", () => {
    renderEdit();
    expect(screen.getByLabelText("Execution Mode")).toHaveValue("cli");
    expect(screen.getByLabelText("Auth Mode")).toHaveValue("oauth");
    expect(screen.getByLabelText("MCP Servers (JSON)")).toHaveValue(
      JSON.stringify(EXISTING.mcp_servers, null, 2),
    );
  });

  it("saves the full surface, sending null to clear an inherited mode", async () => {
    const user = userEvent.setup();
    renderEdit();

    await user.selectOptions(screen.getByLabelText("Execution Mode"), "");
    await user.click(screen.getByRole("button", { name: "Save Agent" }));

    expect(mockUpdateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "developer",
        execution_mode: null,
        auth_mode: "oauth",
        mcp_servers: EXISTING.mcp_servers,
      }),
      expect.anything(),
    );
  });

  it("blocks saving invalid MCP servers JSON", async () => {
    const user = userEvent.setup();
    renderEdit();

    const editor = screen.getByLabelText("MCP Servers (JSON)");
    await user.clear(editor);
    await user.type(editor, "not json");
    await user.click(screen.getByRole("button", { name: "Save Agent" }));

    expect(mockUpdateMutate).not.toHaveBeenCalled();
  });
});
