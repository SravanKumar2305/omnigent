import { describe, expect, it } from "vitest";
import { BlockStream } from "./blockStream";
import { itemsToBlocks } from "./itemsToBlocks";
import { parseEvent } from "./sse";
import { buildBubbles } from "./renderItems";

describe("sub-agent activity history and streaming", () => {
  it.each(["delegated", "returned"])(
    "renders %s metadata identically live and after reload",
    (phase) => {
      const item = {
        id: `activity_${phase}`,
        type: "resource_event",
        status: "completed",
        response_id: "resp_parent",
        event_type: `session.subagent.${phase}`,
        resource_type: "session",
        resource_id: "conv_child",
        resource: { title: "Research public positioning" },
      };
      const event = parseEvent("response.output_item.done", {
        response_id: item.response_id,
        item,
      });
      expect(event).not.toBeNull();
      const live = new BlockStream().reduceSync([event!]);
      const history = itemsToBlocks([item]);
      const liveBubbles = buildBubbles(live, null);
      expect(liveBubbles).toMatchObject([
        {
          kind: "subagent_activity",
          itemId: item.id,
          data: item,
        },
      ]);
      expect(buildBubbles(history, null)).toEqual(liveBubbles);
    },
  );

  it("does not render unrelated or malformed resource events", () => {
    for (const fields of [
      { event_type: "session.resource.created", resource_type: "terminal", resource_id: "term_1" },
      { event_type: "session.subagent.delegated", resource_type: "session", resource_id: "" },
    ]) {
      const item = {
        id: "resource_1",
        type: "resource_event",
        response_id: "resp_parent",
        status: "completed",
        ...fields,
      };
      expect(itemsToBlocks([item])).toEqual([]);
      expect(parseEvent("response.output_item.done", { item })).toBeNull();
    }
  });
});
