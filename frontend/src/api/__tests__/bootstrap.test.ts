import { describe, it, expect, vi, beforeEach } from "vitest";
import type { InternalAxiosRequestConfig } from "axios";

vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

beforeEach(() => {
  vi.resetModules();
});

async function withCapturingAdapter() {
  const { api } = await import("../client");
  const captured: { config?: InternalAxiosRequestConfig } = {};
  api.defaults.adapter = async (config) => {
    captured.config = config;
    return {
      data: {
        repo: "owner/name",
        default_branch: "main",
        private: false,
        qa_config_path: "/store/qa.yaml",
        qa_required: "auto",
        app: null,
        sandbox_profile: null,
        steps: ["GitHub connection verified"],
      },
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
  return captured;
}

describe("bootstrap api", () => {
  it("POSTs to /repos/bootstrap with a widened timeout and the toast opt-out", async () => {
    const captured = await withCapturingAdapter();
    const { bootstrapRepo } = await import("../bootstrap");

    const result = await bootstrapRepo({ repo: "owner/name" });

    expect(result.steps).toEqual(["GitHub connection verified"]);
    expect(captured.config?.url).toBe("/repos/bootstrap");
    expect(captured.config?.method).toBe("post");
    expect(captured.config?.timeout).toBe(60_000);
    expect(captured.config?.suppressValidationToast).toBe(true);
  });

  it("omits empty optional fields from the payload (backend is extra=forbid + slug-validated)", async () => {
    const captured = await withCapturingAdapter();
    const { bootstrapRepo } = await import("../bootstrap");

    await bootstrapRepo({
      repo: "owner/name",
      app: "",
      boot_command: "",
      frontend_url: "",
      ready_check_url: "",
      sandbox_profile: "",
      force: false,
    });

    expect(JSON.parse(captured.config?.data as string)).toEqual({ repo: "owner/name" });
  });

  it("passes populated optional fields through, including force", async () => {
    const captured = await withCapturingAdapter();
    const { bootstrapRepo } = await import("../bootstrap");

    await bootstrapRepo({
      repo: "owner/name",
      app: "web",
      boot_command: "npm run dev",
      sandbox_profile: "slim",
      force: true,
    });

    expect(JSON.parse(captured.config?.data as string)).toEqual({
      repo: "owner/name",
      app: "web",
      boot_command: "npm run dev",
      sandbox_profile: "slim",
      force: true,
    });
  });
});
