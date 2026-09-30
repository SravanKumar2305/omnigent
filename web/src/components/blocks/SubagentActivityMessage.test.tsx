import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { BubbleView } from "@/pages/ChatPage";
import { useChatStore } from "@/store/chatStore";
import type { Bubble } from "@/lib/renderItems";

afterEach(cleanup);

const activity = (
  phase: "delegated" | "returned",
  status?: string,
): Extract<Bubble, { kind: "subagent_activity" }> => ({
  kind: "subagent_activity",
  itemId: `activity_${phase}`,
  data: {
    type: "resource_event",
    event_type: `session.subagent.${phase}`,
    resource_type: "session",
    resource_id: "conv_child",
    resource: { title: "Research public positioning", ...(status ? { status } : {}) },
  },
});

describe("sub-agent activity notices", () => {
  beforeEach(() => useChatStore.setState({ sessionStatus: "idle" }));

  it.each([
    ["delegated", undefined, "Started"],
    ["returned", undefined, "Completed"],
    ["returned", "failed", "Failed"],
    ["returned", "cancelled", "Stopped"],
  ] as const)("renders a %s/%s notice as %s with a child link", (phase, status, label) => {
    render(
      <MemoryRouter
        initialEntries={["/c/parent?file=README.md&view=terminal&message=msg_parent&debug=1"]}
      >
        <BubbleView bubble={activity(phase, status)} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("subagent-activity")).toHaveTextContent(
      `${label} Research public positioning`,
    );
    expect(screen.getByRole("link")).toHaveAttribute("href", "/c/conv_child?debug=1&panel=agents");
    expect(screen.queryByTestId("message-bubble")).not.toBeInTheDocument();
    expect(screen.queryByTestId("message-timestamp")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    expect(screen.queryByText("(ID)")).not.toBeInTheDocument();
  });
});
