import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Binding, Credential } from "@/lib/types/credentials";
import { BindingsDrawer } from "../BindingsDrawer";

const h = vi.hoisted(() => ({
  mutate: vi.fn(),
  bindings: [] as Binding[] | undefined,
  error: null as unknown,
  isError: false,
  isFetching: false,
}));

vi.mock("@/hooks/useCredentials", () => ({
  useCredentials: () => ({ data: [] as Credential[] }),
  useScopedBindings: () => ({ data: h.bindings, isError: h.isError, isFetching: h.isFetching }),
  useSetScopedBindings: () => ({ mutate: h.mutate, error: h.error, isPending: false }),
}));

const credential: Credential = {
  id: "cred-000000000001",
  kind: "forge_token",
  provider: "github",
  host: "github.com",
  label: "alice",
  git_author_name: null,
  git_author_email: null,
  masked_preview: "ghp_••••••••mnop",
  verified_login: null,
  status: "ok",
  status_reason: null,
  quarantined_until: null,
  binding_count: 2,
  created_at: null,
  updated_at: null,
  rotated_at: null,
  last_used_at: null,
};

function otherBinding(over: Partial<Binding> = {}): Binding {
  return {
    scope_kind: "owner",
    scope_value: "github:acme",
    role: "forge",
    provider: "github",
    credential_id: "cred-000000000002",
    pinned: false,
    priority: 0,
    label: "other",
    ...over,
  };
}

describe("BindingsDrawer", () => {
  beforeEach(() => {
    h.mutate.mockClear();
    h.bindings = [];
    h.error = null;
    h.isError = false;
    h.isFetching = false;
  });

  it("posts an owner forge binding through mutate", async () => {
    const user = userEvent.setup();
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    expect(screen.getByRole("dialog", { name: "Bindings for alice" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText(/scope kind/i), "owner");
    const scopeValue = screen.getByLabelText(/scope value/i);
    expect(scopeValue).toHaveValue("github:");
    await user.clear(scopeValue);
    await user.type(scopeValue, "github:acme");
    await user.click(screen.getByRole("button", { name: /add binding/i }));
    expect(h.mutate).toHaveBeenCalledWith({
      scope_kind: "owner",
      scope_value: "github:acme",
      bindings: [{ credential_id: credential.id, role: "forge" }],
    });
    expect(screen.queryByText(credential.masked_preview)).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it("keeps other credentials on the scope when adding this one", async () => {
    h.bindings = [otherBinding()];
    const user = userEvent.setup();
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText(/scope kind/i), "owner");
    const scopeValue = screen.getByLabelText(/scope value/i);
    await user.clear(scopeValue);
    await user.type(scopeValue, "github:acme");
    await user.click(screen.getByRole("button", { name: /add binding/i }));
    expect(h.mutate).toHaveBeenCalledWith({
      scope_kind: "owner",
      scope_value: "github:acme",
      bindings: [
        { credential_id: "cred-000000000002", role: "forge" },
        { credential_id: credential.id, role: "forge" },
      ],
    });
  });

  it("removes this credential and keeps the rest of the scope", async () => {
    h.bindings = [otherBinding(), otherBinding({ credential_id: credential.id, label: "alice" })];
    const user = userEvent.setup();
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText(/scope kind/i), "owner");
    const scopeValue = screen.getByLabelText(/scope value/i);
    await user.clear(scopeValue);
    await user.type(scopeValue, "github:acme");
    await user.click(screen.getByRole("button", { name: /remove from this scope/i }));
    expect(h.mutate).toHaveBeenCalledWith({
      scope_kind: "owner",
      scope_value: "github:acme",
      bindings: [{ credential_id: "cred-000000000002", role: "forge" }],
    });
  });

  it("does not enable save while the scope query is fetching", () => {
    h.isFetching = true;
    h.bindings = [otherBinding({ credential_id: credential.id, label: "alice" })];
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    expect(screen.getByRole("button", { name: /add binding/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /remove from this scope/i })).toBeDisabled();
  });

  it("prefills host from the credential and clears global", async () => {
    const user = userEvent.setup();
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    await user.selectOptions(screen.getByLabelText(/scope kind/i), "host");
    expect(screen.getByLabelText(/scope value/i)).toHaveValue("github.com");
    await user.selectOptions(screen.getByLabelText(/scope kind/i), "global");
    expect(screen.getByLabelText(/scope value/i)).toHaveValue("");
  });

  it("shows a load failure instead of an empty scope", () => {
    h.bindings = undefined;
    h.isError = true;
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load bindings.");
    expect(screen.queryByText(/not bound at this scope/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add binding/i })).toBeDisabled();
  });

  it("disables the form for an operator", async () => {
    const user = userEvent.setup();
    render(<BindingsDrawer credential={credential} isAdmin={false} onClose={vi.fn()} />);
    expect(screen.getByLabelText(/scope kind/i)).toBeDisabled();
    expect(screen.getByLabelText(/scope value/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /add binding/i })).toBeDisabled();
    expect(screen.getByRole("dialog", { name: "Bindings for alice" })).toBeInTheDocument();
    await user.hover(screen.getByRole("button", { name: /add binding/i }));
    expect(screen.getByText("Admin scope required")).toBeInTheDocument();
  });

  it("renders a 422 detail string as an alert", () => {
    h.error = {
      response: {
        status: 422,
        data: {
          detail: "owner scope_value must be 'github:<owner>' or 'gitlab:<owner>', got 'acme'",
        },
      },
    };
    render(<BindingsDrawer credential={credential} isAdmin onClose={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "owner scope_value must be 'github:<owner>' or 'gitlab:<owner>', got 'acme'",
    );
  });
});
