import { describe, it, expect } from "vitest";
import { ALL_JOB_STATUSES } from "../types/jobs";
import { columnForStatus } from "../boardColumns";

describe("ALL_JOB_STATUSES (RAV-1058 — single source for filter dropdowns)", () => {
  it("contains 12 unique statuses", () => {
    expect(ALL_JOB_STATUSES).toHaveLength(12);
    expect(new Set(ALL_JOB_STATUSES).size).toBe(ALL_JOB_STATUSES.length);
  });

  it("every status maps to a board lane", () => {
    for (const status of ALL_JOB_STATUSES) {
      expect(columnForStatus(status), status).toBeTruthy();
    }
  });
});
