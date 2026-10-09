// frontend/src/components/credentials/__tests__/CredentialModal.test.tsx
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CredentialModal } from "../CredentialModal";

const { startXaiDeviceLogin, pollXaiDeviceLogin } = vi.hoisted(() => ({
  startXaiDeviceLogin: vi.fn(),
  pollXaiDeviceLogin: vi.fn(),
}));

vi.mock("@/api/credentials", () => ({
  startXaiDeviceLogin,
  pollXaiDeviceLogin,
}));

describe("CredentialModal", () => {
  it("submits a create payload with a trimmed secret", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined); const user = userEvent.setup();
    render(<CredentialModal modeSpec={{ mode: "create" }} onSubmit={onSubmit} onClose={vi.fn()} gitlabHost="gitlab.example.com" />);
    await user.type(screen.getByLabelText(/label/i), "alice");
    await user.type(screen.getByLabelText(/^secret/i), "  ghp_abcdefghijklmnop  ");
    await user.click(screen.getByRole("button", { name: /create/i }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ kind: "forge_token", provider: "github", label: "alice", secret: "ghp_abcdefghijklmnop" }));
  });

  it("blocks a blank secret with an inline error, no submit", async () => {
    const onSubmit = vi.fn(); const user = userEvent.setup();
    render(<CredentialModal modeSpec={{ mode: "create" }} onSubmit={onSubmit} onClose={vi.fn()} gitlabHost={null} />);
    await user.type(screen.getByLabelText(/label/i), "alice");
    await user.click(screen.getByRole("button", { name: /create/i }));
    expect(screen.getByRole("alert")).toHaveTextContent(/secret/i);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("clears the secret field after a failed submit", async () => {
    const onSubmit = vi.fn().mockRejectedValue({ response: { status: 409, data: { detail: "a github credential labelled 'alice' already exists" } } });
    const user = userEvent.setup();
    render(<CredentialModal modeSpec={{ mode: "create" }} onSubmit={onSubmit} onClose={vi.fn()} gitlabHost={null} />);
    await user.type(screen.getByLabelText(/label/i), "alice");
    await user.type(screen.getByLabelText(/^secret/i), "ghp_abcdefghijklmnop");
    await user.click(screen.getByRole("button", { name: /create/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already exists/);
    expect(screen.getByLabelText(/^secret/i)).toHaveValue("");
  });

  it("gitlab option is disabled without a configured host", () => {
    render(<CredentialModal modeSpec={{ mode: "create" }} onSubmit={vi.fn()} onClose={vi.fn()} gitlabHost={null} />);
    expect(screen.getByRole("option", { name: /gitlab/i })).toBeDisabled();
  });

  it("submits a Claude API key as an anthropic llm_api_key", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<CredentialModal modeSpec={{ mode: "create" }} onSubmit={onSubmit} onClose={vi.fn()} gitlabHost={null} />);
    await user.selectOptions(screen.getByLabelText(/^kind/i), "claude_api_key");
    await user.type(screen.getByLabelText(/label/i), "claude-default");
    await user.type(screen.getByLabelText(/^secret/i), "sk-ant-placeholder");
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    expect(onSubmit).toHaveBeenCalledWith({
      kind: "llm_api_key",
      provider: "anthropic",
      label: "claude-default",
      secret: "sk-ant-placeholder",
    });
  });

  it("polls xAI device login on the interval from the start response", async () => {
    vi.useFakeTimers();
    startXaiDeviceLogin.mockResolvedValue({
      login_id: "login-1",
      user_code: "ABCD-EFGH",
      verification_uri_complete: "https://x.ai/device?user_code=ABCD-EFGH",
      expires_at: "2026-10-01T00:05:00Z",
      interval: 5,
    });
    pollXaiDeviceLogin
      .mockResolvedValueOnce({ status: "pending" })
      .mockResolvedValueOnce({ status: "done", credential: { id: "cred-00000000000b" } });
    const onSubmit = vi.fn();
    render(<CredentialModal modeSpec={{ mode: "create" }} onSubmit={onSubmit} onClose={vi.fn()} gitlabHost={null} />);
    fireEvent.change(screen.getByLabelText(/label/i), { target: { value: "grok-default" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in with xai/i }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(startXaiDeviceLogin).toHaveBeenCalledWith("grok-default");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("ABCD-EFGH")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /x\.ai/i });
    expect(link).toHaveAttribute("href", "https://x.ai/device?user_code=ABCD-EFGH");
    expect(link).toHaveAttribute("target", "_blank");
    fireEvent.mouseOver(link);
    expect(screen.getByText("opens x.ai in a new tab")).toBeInTheDocument();
    expect(pollXaiDeviceLogin).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4999);
    });
    expect(pollXaiDeviceLogin).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(pollXaiDeviceLogin).toHaveBeenCalledTimes(1);
    expect(pollXaiDeviceLogin).toHaveBeenCalledWith("login-1");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(pollXaiDeviceLogin).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(pollXaiDeviceLogin).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("rotate mode shows the label read-only and submits only the secret", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined); const user = userEvent.setup();
    const credential = { id: "cred-000000000001", label: "alice", provider: "github", host: "github.com" } as never;
    render(<CredentialModal modeSpec={{ mode: "rotate", credential }} onSubmit={onSubmit} onClose={vi.fn()} gitlabHost={null} />);
    await user.type(screen.getByLabelText(/new secret/i), "ghp_zzzzzzzzzzzzzzzz");
    await user.click(screen.getByRole("button", { name: /rotate/i }));
    expect(onSubmit).toHaveBeenCalledWith({ secret: "ghp_zzzzzzzzzzzzzzzz" });
  });
});

afterEach(() => {
  vi.useRealTimers();
  startXaiDeviceLogin.mockReset();
  pollXaiDeviceLogin.mockReset();
});
