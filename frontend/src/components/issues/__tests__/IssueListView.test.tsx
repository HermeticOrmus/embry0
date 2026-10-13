import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { IssueListView } from "../IssueListView";

function renderList(onStatusChange = vi.fn()) {
  render(
    <MemoryRouter>
      <IssueListView
        issues={[]}
        total={0}
        offset={0}
        limit={20}
        filters={{}}
        repoOptions={[]}
        onStatusChange={onStatusChange}
        onPriorityChange={vi.fn()}
        onRepoChange={vi.fn()}
        onSearchChange={vi.fn()}
        searchValue=""
        onPageChange={vi.fn()}
      />
    </MemoryRouter>,
  );
  return onStatusChange;
}

describe("IssueListView — status filter (#450)", () => {
  it("can filter to awaiting_input, the status that needs a person", async () => {
    const onStatusChange = renderList();
    const select = screen.getByRole("combobox", { name: "Statuses" });

    const values = Array.from((select as HTMLSelectElement).options).map((o) => o.value);
    expect(values, "every IssueStatus must be selectable in the list filter").toEqual([
      "",
      "open",
      "triaging",
      "awaiting_input",
      "in_progress",
      "closed",
      "cancelled",
    ]);

    await userEvent.selectOptions(select, "awaiting_input");
    expect(onStatusChange).toHaveBeenCalledWith("awaiting_input");
  });
});
