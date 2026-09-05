import { expect, it, vi } from "vitest";
import { clearConversationId, readConversationId, writeConversationId } from "./session";

it("reads, writes, and clears a conversation id", () => {
  expect(readConversationId()).toBeNull();
  writeConversationId("conversation-1");
  expect(readConversationId()).toBe("conversation-1");
  clearConversationId();
  expect(readConversationId()).toBeNull();
});

it("does not propagate storage failures", () => {
  const failure = new Error("storage disabled");
  vi.stubGlobal("sessionStorage", {
    getItem: () => {
      throw failure;
    },
    setItem: () => {
      throw failure;
    },
    removeItem: () => {
      throw failure;
    },
  });
  expect(readConversationId()).toBeNull();
  expect(() => writeConversationId("conversation-1")).not.toThrow();
  expect(() => clearConversationId()).not.toThrow();
});
