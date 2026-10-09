import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router";

// Stub the layout sub-components so this test focuses on the shell contract
// rather than re-asserting sidebar/topbar behavior (covered elsewhere).
vi.mock("../Sidebar", () => ({
  Sidebar: () => <div data-testid="stub-sidebar" />,
}));
vi.mock("../TopBar", () => ({
  TopBar: () => <div data-testid="stub-topbar" />,
}));
vi.mock("@/stores/layoutStore", () => ({
  useLayoutStore: (selector?: (s: { densityMode: string }) => unknown) =>
    selector ? selector({ densityMode: "comfortable" }) : { densityMode: "comfortable" },
}));

import { AppLayout } from "../AppLayout";

describe("AppLayout", () => {
  it("renders sidebar, topbar, and the routed outlet content", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route index element={<div>home</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("stub-sidebar")).toBeInTheDocument();
    expect(screen.getByTestId("stub-topbar")).toBeInTheDocument();
    expect(screen.getByText("home")).toBeInTheDocument();
  });

  it("mounts no Companion surfaces (FAB, palette, notifications) — RAV-1058 A2 drop", () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route index element={<div>home</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("feedback-fab")).toBeNull();
    expect(screen.queryByTestId("command-palette")).toBeNull();
  });
});
