import test from "node:test";
import assert from "node:assert/strict";
import { isEditorFrameMessage, requestEditorDocument } from "../src/editor-frame-bridge.js";

function createWindowRef() {
  const listeners = new Set();
  return {
    addEventListener: (type, listener) => type === "message" && listeners.add(listener),
    removeEventListener: (type, listener) => type === "message" && listeners.delete(listener),
    setTimeout,
    clearTimeout,
    dispatch(event) { for (const listener of [...listeners]) listener(event); },
    get listenerCount() { return listeners.size; },
  };
}

test("reads editor content only from the sandboxed frame with a matching request id", async () => {
  const windowRef = createWindowRef();
  const frame = { contentWindow: { postMessage(message, targetOrigin) {
    assert.equal(targetOrigin, "*");
    windowRef.dispatch({ source: {}, origin: "null", data: { type: "iscan:editor-document", requestId: message.requestId, payload: "spoofed" } });
    windowRef.dispatch({ source: frame.contentWindow, origin: "https://example.test", data: { type: "iscan:editor-document", requestId: message.requestId, payload: "wrong origin" } });
    windowRef.dispatch({ source: frame.contentWindow, origin: "null", data: { type: "iscan:editor-document", requestId: "wrong-id", payload: "wrong request" } });
    windowRef.dispatch({ source: frame.contentWindow, origin: "null", data: { type: "iscan:editor-document", requestId: message.requestId, payload: "{\"paragraphs\":[\"safe\"]}" } });
  } } };

  assert.equal(await requestEditorDocument(frame, windowRef), '{"paragraphs":["safe"]}');
  assert.equal(windowRef.listenerCount, 0);
});

test("rejects editor reads that time out and removes the pending listener", async () => {
  const windowRef = createWindowRef();
  const frame = { contentWindow: { postMessage() {} } };

  await assert.rejects(requestEditorDocument(frame, windowRef, 1), /超时/);
  assert.equal(windowRef.listenerCount, 0);
});

test("accepts dirty notifications only from the sandboxed editor frame", () => {
  const frame = { contentWindow: {} };
  assert.equal(isEditorFrameMessage({ source: frame.contentWindow, origin: "null", data: { type: "iscan:editor-dirty" } }, frame), true);
  assert.equal(isEditorFrameMessage({ source: frame.contentWindow, origin: "https://example.test", data: { type: "iscan:editor-dirty" } }, frame), false);
  assert.equal(isEditorFrameMessage({ source: {}, origin: "null", data: { type: "iscan:editor-dirty" } }, frame), false);
});
