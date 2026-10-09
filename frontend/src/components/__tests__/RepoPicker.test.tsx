import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetchRepos = vi.fn();

vi.mock("@/api/repos", () => ({
  fetchRepos: () => mockFetchRepos(),
}));

import { RepoPicker } from "../RepoPicker";
import { defaultRepoAnnotation } from "@/hooks/useRepos";

const REPOS = [
  {
    repo: "acme/backend",
    active: true,
    notes: "",
    created_at: "",
    updated_at: "",
    forge: "github" as const,
    has_qa_config: true,
    docker_access: false,
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
    docker_access: false,
    env_var_count: 22,
    env_secret_count: 20,
  },
  {
    repo: "acme/empty",
    active: true,
    notes: "",
    created_at: "",
    updated_at: "",
    forge: "github" as const,
    has_qa_config: false,
    docker_access: false,
    env_var_count: 0,
    env_secret_count: 0,
  },
];

function renderPicker(props: { value: string; onChange: (r: string) => void }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RepoPicker {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFetchRepos.mockResolvedValue(REPOS);
});

describe("RepoPicker", () => {
  it("lists registry repos annotated with env-var counts", async () => {
    renderPicker({ value: "", onChange: vi.fn() });
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "acme/console (22 vars, 20 secrets)" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("option", { name: "acme/backend (2 vars, 2 secrets)" }),
    ).toBeInTheDocument();
    // Repos without variables show no annotation.
    expect(screen.getByRole("option", { name: "acme/empty" })).toBeInTheDocument();
  });

  it("fires onChange with the selected repo", async () => {
    const onChange = vi.fn();
    renderPicker({ value: "", onChange });
    await waitFor(() => expect(screen.getAllByRole("option").length).toBeGreaterThan(1));
    await userEvent.selectOptions(screen.getByRole("combobox"), "acme/console");
    expect(onChange).toHaveBeenCalledWith("acme/console");
  });

  it("keeps a deep-linked unregistered repo selectable", async () => {
    renderPicker({ value: "other/legacy", onChange: vi.fn() });
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "other/legacy (unregistered)" })).toBeInTheDocument(),
    );
    expect(screen.getByRole("combobox")).toHaveValue("other/legacy");
  });
});

describe("defaultRepoAnnotation", () => {
  it("pluralises correctly and omits zero-secret suffix", () => {
    expect(defaultRepoAnnotation({ ...REPOS[0], env_var_count: 1, env_secret_count: 1 })).toBe(
      "acme/backend (1 var, 1 secret)",
    );
    expect(defaultRepoAnnotation({ ...REPOS[0], env_var_count: 3, env_secret_count: 0 })).toBe(
      "acme/backend (3 vars)",
    );
    expect(defaultRepoAnnotation({ ...REPOS[2] })).toBe("acme/empty");
  });
});
