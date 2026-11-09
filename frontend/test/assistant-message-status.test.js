import assert from "node:assert/strict";
import test from "node:test";
import { resolveAssistantMessageStatus } from "../src/assistant-message-status.js";

const message = { id: "reply-1", role: "assistant", status: "streaming" };

test("reports streaming only for the assistant response active in this conversation", () => {
  assert.equal(resolveAssistantMessageStatus(message, {
    sending: true,
    activeConversationId: "chat-1",
    requestConversationId: "chat-1",
    streamingMessageId: "reply-1",
  }), "streaming");
});

test("shows persisted streaming messages as interrupted after the request is gone", () => {
  assert.equal(resolveAssistantMessageStatus(message, {
    sending: false,
    activeConversationId: "chat-1",
    requestConversationId: "",
    streamingMessageId: "",
  }), "interrupted");
  assert.equal(resolveAssistantMessageStatus(message, {
    sending: true,
    activeConversationId: "chat-2",
    requestConversationId: "chat-1",
    streamingMessageId: "reply-1",
  }), "interrupted");
});

test("preserves completed and interrupted message states", () => {
  assert.equal(resolveAssistantMessageStatus({ status: "complete" }, {}), "complete");
  assert.equal(resolveAssistantMessageStatus({ status: "interrupted" }, {}), "interrupted");
  assert.equal(resolveAssistantMessageStatus({}, {}), "");
});
