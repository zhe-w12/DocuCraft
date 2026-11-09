import assert from "node:assert/strict";
import test from "node:test";
import { latestAssistantMessage, latestDocumentMessageId, shouldOpenDocumentPane } from "../src/assistant-layout.js";

test("auto-routed chat keeps the document pane closed", () => {
  assert.equal(shouldOpenDocumentPane("auto", "chat"), false);
});

test("explicit chat keeps the document pane closed", () => {
  assert.equal(shouldOpenDocumentPane("chat", "write"), false);
});

test("document workflows keep the document pane open", () => {
  assert.equal(shouldOpenDocumentPane("auto", "write"), true);
  assert.equal(shouldOpenDocumentPane("write", "chat"), true);
  assert.equal(shouldOpenDocumentPane("auto", undefined), true);
});

test("only the latest assistant document reply gets the current document action", () => {
  const messages = [
    { id: "older-draft", role: "assistant", workflow: "write" },
    { id: "chat-reply", role: "assistant", workflow: "chat" },
    { id: "latest-draft", role: "assistant", workflow: "revise" },
    { id: "later-user-message", role: "user", workflow: "write" },
  ];

  assert.equal(latestDocumentMessageId(messages), "latest-draft");
  assert.equal(latestDocumentMessageId([{ id: "chat", role: "assistant", workflow: "chat" }]), null);
});

test("finds the last assistant reply from the end without copying the message array", () => {
  const messages = new Proxy([
    { id: "older-reply", role: "assistant" },
    { id: "latest-user", role: "user" },
    { id: "latest-reply", role: "assistant" },
  ], {
    get(target, key, receiver) {
      if (key === Symbol.iterator) throw new Error("The history must not be copied or iterated from the start");
      return Reflect.get(target, key, receiver);
    },
  });

  assert.equal(latestAssistantMessage(messages).id, "latest-reply");
  assert.equal(latestAssistantMessage([{ id: "user", role: "user" }]), null);
  assert.equal(latestAssistantMessage(null), null);
});
