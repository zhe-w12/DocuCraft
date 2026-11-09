import assert from "node:assert/strict";
import test from "node:test";
import { createAssistantMessageMarkdownRenderer } from "../src/assistant-message-markdown.js";

function createRendererHarness() {
  const rendered = [];
  const mathRequests = [];
  let rendererReady = false;
  const renderMessage = createAssistantMessageMarkdownRenderer({
    render: content => {
      rendered.push(content);
      return `<p>${content}</p>`;
    },
    requestMathRenderer: content => mathRequests.push(content),
    isRendererReady: () => rendererReady,
  });

  return {
    mathRequests,
    renderMessage,
    rendered,
    setRendererReady(value) { rendererReady = value; },
  };
}

test("reuses rendered HTML for unchanged historical messages", () => {
  const harness = createRendererHarness();
  const message = { content: "旧消息" };

  assert.equal(harness.renderMessage(message), "<p>旧消息</p>");
  assert.equal(harness.renderMessage(message), "<p>旧消息</p>");
  assert.deepEqual(harness.rendered, ["旧消息"]);
  assert.deepEqual(harness.mathRequests, ["旧消息"]);
});

test("rerenders a streaming message when its content changes", () => {
  const harness = createRendererHarness();
  const message = { content: "生成到一半" };

  assert.equal(harness.renderMessage(message), "<p>生成到一半</p>");
  message.content = "生成到一半，继续完成";
  assert.equal(harness.renderMessage(message), "<p>生成到一半，继续完成</p>");
  assert.deepEqual(harness.rendered, ["生成到一半", "生成到一半，继续完成"]);
});

test("rerenders cached messages after the math renderer becomes available", () => {
  const harness = createRendererHarness();
  const message = { content: "公式 $x^2$" };

  assert.equal(harness.renderMessage(message), "<p>公式 $x^2$</p>");
  harness.setRendererReady(true);
  assert.equal(harness.renderMessage(message), "<p>公式 $x^2$</p>");
  assert.deepEqual(harness.rendered, ["公式 $x^2$", "公式 $x^2$"]);
});
