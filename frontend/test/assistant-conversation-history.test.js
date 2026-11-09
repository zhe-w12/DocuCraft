import assert from "node:assert/strict";
import test from "node:test";
import {
  ASSISTANT_DIALOG_HISTORY_PAGE_SIZE,
  ASSISTANT_SIDEBAR_HISTORY_LIMIT,
  nextAssistantConversationHistoryLimit,
  orderAssistantConversationHistory,
  sliceAssistantConversationHistory,
} from "../src/assistant-conversation-history.js";

test("orders nonempty conversations by recent activity without mutating storage state", () => {
  const conversations = [
    { id: "older", updatedAt: 10, messages: [{ role: "user" }] },
    { id: "empty", updatedAt: 50, messages: [] },
    { id: "newest", updatedAt: 30, messages: [{ role: "user" }] },
  ];

  const history = orderAssistantConversationHistory(conversations);
  assert.deepEqual(history.map(item => item.id), ["newest", "older"]);
  assert.deepEqual(conversations.map(item => item.id), ["older", "empty", "newest"]);
});

test("bounds the sidebar and exposes every retained conversation through pages", () => {
  const conversations = Array.from({ length: 95 }, (_, index) => ({
    id: `chat-${index}`,
    updatedAt: index,
    messages: [{ role: "user", content: `问题 ${index}` }],
  }));
  const history = orderAssistantConversationHistory(conversations);
  const sidebar = sliceAssistantConversationHistory(history, ASSISTANT_SIDEBAR_HISTORY_LIMIT);
  const firstPage = sliceAssistantConversationHistory(history, ASSISTANT_DIALOG_HISTORY_PAGE_SIZE);
  const nextLimit = nextAssistantConversationHistoryLimit(firstPage.length, history.length);
  const nextPage = sliceAssistantConversationHistory(history, nextLimit);
  const finalLimit = nextAssistantConversationHistoryLimit(nextLimit, history.length);
  const finalPage = sliceAssistantConversationHistory(history, finalLimit);

  assert.equal(sidebar.length, 8);
  assert.equal(sidebar[0].id, "chat-94");
  assert.equal(firstPage.length, 40);
  assert.equal(nextPage.length, 80);
  assert.equal(nextPage[79].id, "chat-15");
  assert.equal(finalPage.length, history.length);
  assert.equal(finalPage.at(-1).id, "chat-0");
  assert.equal(history.length, conversations.length);
});

test("clamps pagination limits for invalid and exhausted histories", () => {
  assert.equal(nextAssistantConversationHistoryLimit(-1, 100), 40);
  assert.equal(nextAssistantConversationHistoryLimit(40, 60), 60);
  assert.equal(nextAssistantConversationHistoryLimit("invalid", 0), 0);
});
