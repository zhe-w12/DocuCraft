import assert from "node:assert/strict";
import test from "node:test";
import { handleAssistantComposerKeydown, resolveAssistantDraftOnConversationChange } from "../src/assistant-composer.js";

function keyEvent(overrides = {}) {
  return {
    key: "Enter",
    shiftKey: false,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    isComposing: false,
    keyCode: 13,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    ...overrides,
  };
}

test("submits on plain Enter and prevents its newline", () => {
  const event = keyEvent();
  let submissions = 0;

  assert.equal(handleAssistantComposerKeydown(event, () => { submissions += 1; }), true);
  assert.equal(event.defaultPrevented, true);
  assert.equal(submissions, 1);
});

test("leaves modified Enter and IME composition events to the browser", () => {
  for (const overrides of [
    { key: "a" },
    { shiftKey: true },
    { ctrlKey: true },
    { altKey: true },
    { metaKey: true },
    { isComposing: true },
    { keyCode: 229 },
  ]) {
    const event = keyEvent(overrides);
    let submissions = 0;

    assert.equal(handleAssistantComposerKeydown(event, () => { submissions += 1; }), false);
    assert.equal(event.defaultPrevented, false, `should not prevent default for ${JSON.stringify(overrides)}`);
    assert.equal(submissions, 0, `should not submit for ${JSON.stringify(overrides)}`);
  }
});

test("keeps an unassigned home draft when its first conversation is created", () => {
  assert.deepEqual(resolveAssistantDraftOnConversationChange({
    previousConversationId: null,
    draftConversationId: "",
    currentDraft: "先写好的问题",
    nextDraft: "",
  }), {
    value: "先写好的问题",
    preserveUnassignedDraft: true,
  });
});

test("loads the target conversation draft instead of carrying another conversation's draft", () => {
  assert.deepEqual(resolveAssistantDraftOnConversationChange({
    previousConversationId: "conversation-a",
    draftConversationId: "conversation-a",
    currentDraft: "A 的草稿",
    nextDraft: "B 的草稿",
  }), {
    value: "B 的草稿",
    preserveUnassignedDraft: false,
  });
});
