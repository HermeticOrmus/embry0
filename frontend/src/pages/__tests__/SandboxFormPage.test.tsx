import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";

const mockUseProfile = vi.fn();
const mockUpdateMutate = vi.fn();
const mockCreateMutate = vi.fn();

vi.mock("@/hooks/useSandboxProfiles", () => ({
  useSandboxProfile: (name: string | null) => mockUseProfile(name),
  useCreateSandboxProfile: () => ({ mutate: mockCreateMutate, isPending: false }),
  useUpdateSandboxProfile: () => ({ mutate: mockUpdateMutate, isPending: false }),
  useResetSandboxProfile: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { SandboxFormPage } from "../SandboxFormPage";

// A custom profile stored before the API refused weakening values (#553).
const LEGACY = {
  name: "custom",
  base_image: "embry0-sandbox:latest",
  additional_packages: [],
  setup_commands: [],
  memory: "8g",
  cpus: "4",
  pids_limit: 256,
  cap_drop: ["NET_RAW"],
  cap_add: ["ALL"],
  security_opt: ["seccomp=unconfined"],
  agent_timeout_seconds: 300,
  container_timeout_seconds: 3600,
  description: "",
  dind_enabled: false,
  idle_timeout_seconds: 600,
  extra_networks: ["bridge", "sandbox-internet"],
  env_defaults: {},
  extra_hosts: {},
  is_builtin: false,
};

function renderEdit() {
  return render(
    <MemoryRouter initialEntries={["/sandboxes/custom"]}>
      <Routes>
        <Route path="/sandboxes/:name" element={<SandboxFormPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseProfile.mockReturnValue({ data: LEGACY, isLoading: false });
});

describe("SandboxFormPage — isolation fields (#553)", () => {
  it("offers no input for capabilities or security options", () => {
    renderEdit();
    expect(screen.queryByText("cap_add")).toBeNull();
    expect(screen.queryByText("security_opt")).toBeNull();
    expect(screen.getByText("--cap-drop=ALL")).toBeInTheDocument();
  });

  it("saves the fixed isolation values over a legacy profile's", async () => {
    const user = userEvent.setup();
    renderEdit();

    await user.click(screen.getByRole("button", { name: "Save Profile" }));

    expect(mockUpdateMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        cap_drop: ["ALL"],
        cap_add: [],
        security_opt: ["no-new-privileges"],
        extra_networks: ["sandbox-internet"],
      }),
      expect.anything(),
    );
  });

  it("joins no extra network once internet egress is unticked", async () => {
    const user = userEvent.setup();
    renderEdit();

    const egress = screen.getByLabelText("Internet egress (sandbox-internet)");
    expect(egress).toBeChecked();
    await user.click(egress);
    await user.click(screen.getByRole("button", { name: "Save Profile" }));

    expect(mockUpdateMutate).toHaveBeenCalledWith(
      expect.objectContaining({ extra_networks: [] }),
      expect.anything(),
    );
  });
});
