import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  ASSISTANT_MESSAGE_HISTORY_PAGE_SIZE,
  INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT,
  nextAssistantMessageHistoryLimit,
  sliceLatestAssistantMessages,
} from "../src/assistant-message-history.js";

test("shows the latest messages first while keeping older messages available", () => {
  const messages = Array.from({ length: 105 }, (_, index) => ({ id: `message-${index}` }));
  const visible = sliceLatestAssistantMessages(messages, INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT);

  assert.equal(visible.length, 40);
  assert.equal(visible[0].id, "message-65");
  assert.equal(visible.at(-1).id, "message-104");
  assert.equal(messages.length, 105);
});

test("loads older messages in bounded pages until the first message is visible", () => {
  const messages = Array.from({ length: 105 }, (_, index) => ({ id: `message-${index}` }));
  const nextLimit = nextAssistantMessageHistoryLimit(INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT, messages.length);
  const visible = sliceLatestAssistantMessages(messages, nextLimit);
  const finalLimit = nextAssistantMessageHistoryLimit(nextLimit, messages.length);

  assert.equal(ASSISTANT_MESSAGE_HISTORY_PAGE_SIZE, 40);
  assert.equal(visible[0].id, "message-25");
  assert.equal(finalLimit, messages.length);
  assert.equal(sliceLatestAssistantMessages(messages, finalLimit)[0].id, "message-0");
});

test("handles empty input and invalid limits without returning all history", () => {
  assert.deepEqual(sliceLatestAssistantMessages(null, 40), []);
  assert.deepEqual(sliceLatestAssistantMessages([{ id: "one" }], Number.NaN), []);
  assert.equal(nextAssistantMessageHistoryLimit("bad", 12), 12);
  assert.equal(nextAssistantMessageHistoryLimit(40, 12), 12);
});

test("pages only transcript rendering while requests still use the conversation history", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");

  assert.match(workspace, /v-for="message in visibleMessages"/);
  assert.doesNotMatch(workspace, /v-for="message in activeMessages"/);
  assert.match(workspace, /hasOlderMessages[^>]*@click="loadOlderMessages"/);
  assert.match(workspace, /messages: buildAssistantRequestMessages\(conversation\)/);
  assert.match(workspace, /visibleMessageCount\.value = INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT/);
  assert.match(workspace, /scrollAdjustmentForAnchor\(anchorTop, anchor\.getBoundingClientRect\(\)\.top\)/);
});
