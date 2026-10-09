import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { AgentFailuresCard } from "../AgentFailuresCard";

describe("AgentFailuresCard", () => {
  it("renders nothing when the job has no errored runs", () => {
    const { container } = render(<AgentFailuresCard failures={[]} />);
    expect(container).toBeEmptyDOMElement();
    render(<AgentFailuresCard failures={undefined} />);
    expect(screen.queryByTestId("agent-failures")).toBeNull();
  });

  it("shows each run's agent, code and stderr tail", () => {
    render(
      <AgentFailuresCard
        failures={[
          {
            trace_id: "trc-1",
            agent_type: "developer",
            error_code: "ERR_AGENT_PROCESS_FAILED",
            result_summary: "Command failed with exit code 1",
            stderr_tail: "ENOSPC: no space left on device",
          },
          { trace_id: "trc-2", agent_type: "review", error_code: null, stderr_tail: null },
        ]}
      />,
    );

    expect(screen.getByText("Agent run failures (2)")).toBeInTheDocument();
    expect(screen.getAllByTestId("agent-failure")).toHaveLength(2);
    expect(screen.getByText("ERR_AGENT_PROCESS_FAILED")).toBeInTheDocument();
    expect(screen.getByText("ENOSPC: no space left on device")).toBeInTheDocument();
  });

  it("tolerates a code this build has never heard of", () => {
    render(
      <AgentFailuresCard
        failures={[{ trace_id: "trc-3", agent_type: "qa", error_code: "ERR_FROM_THE_FUTURE" }]}
      />,
    );
    expect(screen.getByText("ERR_FROM_THE_FUTURE")).toBeInTheDocument();
  });
});
