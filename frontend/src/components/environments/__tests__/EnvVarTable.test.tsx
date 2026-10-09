import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { EnvVarTable } from "../EnvVarTable";
import type { EnvVar } from "@/lib/types/environment";

const GLYPHS = "•".repeat(8);

const VARS: EnvVar[] = [
  {
    key: "AZURE_KEY",
    value: "****",
    var_type: "secret",
    description: "",
    masked_preview: `AZUR${GLYPHS}7Q2F`,
  },
  {
    key: "OLD_SECRET",
    value: "****",
    var_type: "secret",
    description: "",
    masked_preview: null,
  },
  { key: "MODE", value: "prod", var_type: "config", description: "" },
];

describe("EnvVarTable — RAV-1294 partial previews", () => {
  it("shows the partial preview for secrets that have one", () => {
    render(<EnvVarTable variables={VARS} onEdit={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText(`AZUR${GLYPHS}7Q2F`)).toBeInTheDocument();
  });

  it("falls back to the full mask when no preview exists", () => {
    render(<EnvVarTable variables={VARS} onEdit={vi.fn()} onDelete={vi.fn()} />);
    expect(screen.getByText(GLYPHS)).toBeInTheDocument();
  });

  it("shows the revealed value instead of the preview once revealed", () => {
    render(
      <EnvVarTable
        variables={VARS}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
        onReveal={vi.fn()}
        revealedValues={{ AZURE_KEY: "AZURrealvalue7Q2F" }}
      />,
    );
    expect(screen.getByText("AZURrealvalue7Q2F")).toBeInTheDocument();
    expect(screen.queryByText(`AZUR${GLYPHS}7Q2F`)).not.toBeInTheDocument();
  });
});
