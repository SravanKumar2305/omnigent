import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { BubbleView } from "@/pages/ChatPage";
import { useChatStore } from "@/store/chatStore";
import { buildBubbles, type Bubble } from "@/lib/renderItems";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import type { ConversationItem } from "@/lib/conversationItems";

afterEach(cleanup);

const activity = (
  phase: "delegated" | "returned",
): Extract<Bubble, { kind: "subagent_activity" }> => ({
  kind: "subagent_activity",
  itemId: `activity_${phase}`,
  data: {
    type: "resource_event",
    event_type: `session.subagent.${phase}`,
    resource_type: "session",
    resource_id: "conv_child",
    resource: { title: "Research public positioning" },
  },
});

describe("sub-agent activity notices", () => {
  beforeEach(() => useChatStore.setState({ sessionStatus: "idle" }));

  it("keeps both linked lifecycle notices visible outside the completed work fold", () => {
    render(
      <MemoryRouter
        initialEntries={["/c/parent?file=README.md&view=terminal&message=msg_parent&debug=1"]}
      >
        <TooltipProvider>
          <BubbleView
            bubble={{
              kind: "assistant",
              responseId: "resp_parent",
              stableId: "resp_parent",
              lifecycle: "completed",
              error: null,
              workedForS: 4,
              items: [
                { kind: "reasoning", itemId: "reasoning", text: "Private work trace", duration: 1 },
                { kind: "text", itemId: "answer", text: "Here are the findings.", final: true },
              ],
            }}
          />
          <BubbleView bubble={activity("delegated")} />
          <BubbleView bubble={activity("returned")} />
        </TooltipProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText("Worked for 4s")).toBeVisible();
    expect(screen.queryByText("Private work trace")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("subagent-activity")).toHaveLength(2);
    expect(screen.getAllByTestId("subagent-activity")[0]).toHaveTextContent(
      "Started Research public positioning",
    );
    expect(screen.getAllByTestId("subagent-activity")[1]).toHaveTextContent(
      "Completed Research public positioning",
    );
    for (const link of screen.getAllByRole("link")) {
      expect(link).toBeVisible();
      expect(link).toHaveAttribute("href", "/c/conv_child?debug=1&panel=agents");
    }
    expect(screen.getByText("Here are the findings.")).toBeVisible();
    expect(screen.queryByText("(ID)")).not.toBeInTheDocument();
  });

  it("keeps settled work folded when a returned notice arrives after the answer", () => {
    const items: ConversationItem[] = [
      {
        id: "reasoning_1",
        type: "reasoning",
        status: "completed",
        response_id: "resp_parent",
        created_at: 1_750_000_000,
        model: "parent",
        summary: [{ type: "summary_text", text: "Private work trace" }],
      },
      {
        id: "answer_1",
        type: "message",
        status: "completed",
        response_id: "resp_parent",
        created_at: 1_750_000_004,
        role: "assistant",
        content: [{ type: "output_text", text: "The review is complete." }],
      },
    ];
    const timeline = (history: ConversationItem[]) => (
      <MemoryRouter>
        {buildBubbles(itemsToBlocks(history), null).map((bubble) => (
          <BubbleView
            key={bubble.kind === "assistant" ? bubble.stableId : bubble.itemId}
            bubble={bubble}
          />
        ))}
      </MemoryRouter>
    );
    const { rerender } = render(timeline(items));
    expect(screen.getByText("Worked for 4s")).toBeVisible();
    rerender(
      timeline([
        ...items,
        {
          id: "returned_1",
          response_id: "subagent_returned",
          status: "completed",
          created_at: 1_750_000_005,
          ...activity("returned").data,
        } as ConversationItem,
      ]),
    );

    expect(screen.getByText("Worked for 4s")).toBeVisible();
    expect(screen.queryByText("Private work trace")).not.toBeInTheDocument();
    expect(screen.getByText("The review is complete.")).toBeVisible();
    expect(screen.getByTestId("subagent-activity")).toHaveTextContent("Completed");
    expect(screen.getAllByTestId("message-bubble")).toHaveLength(1);
  });

  it.each([
    ["failed", "Failed"],
    ["cancelled", "Stopped"],
  ])("does not label a %s child as completed", (status, label) => {
    const bubble = activity("returned");
    bubble.data.resource = { title: "Research public positioning", status };
    render(
      <MemoryRouter>
        <BubbleView bubble={bubble} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("subagent-activity")).toHaveTextContent(
      `${label} Research public positioning`,
    );
  });
});
