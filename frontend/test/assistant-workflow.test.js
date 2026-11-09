import assert from "node:assert/strict";
import test from "node:test";
import { resolveAssistantWorkflow } from "../src/assistant-workflow.js";

test("auto routes greetings and ordinary questions to chat", () => {
  assert.equal(resolveAssistantWorkflow("auto", "你好"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "你能做什么？"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "怎么把标题改得自然一些？"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "给我讲讲这个项目的思路"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "请给我讲讲模型生成原理"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "介绍一下怎么写报告"), "chat");
});

test("auto keeps explicit writing and editing requests in the document workflow", () => {
  assert.equal(resolveAssistantWorkflow("auto", "帮我写一份项目总结"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "生成微积分核心知识点"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "润色这段文字"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "你能帮我写一份邮件吗？"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "介绍一下，然后帮我写一份邮件"), "auto");
});

test("auto recognizes planning and explicit write-as-a-task phrasing", () => {
  assert.equal(resolveAssistantWorkflow("auto", "帮我构思一篇演讲稿"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "帮我制定一个每周学习计划"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "请规划一份成都三日游行程"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "请帮我写作一篇文章"), "auto");
  assert.equal(resolveAssistantWorkflow("auto", "帮我写作"), "auto");
});

test("auto keeps writing-related questions and explanations in chat", () => {
  assert.equal(resolveAssistantWorkflow("auto", "我想学习写作技巧"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "帮我解释这段代码"), "chat");
  assert.equal(resolveAssistantWorkflow("auto", "如何构思一篇演讲稿？"), "chat");
});

test("auto continues an existing document task when the user asks to continue", () => {
  const messages = [{ role: "assistant", workflow: "auto", content: "# 项目总结" }];
  assert.equal(resolveAssistantWorkflow("auto", "继续补完结尾", messages), "auto");
});

test("explicit workflow selection takes precedence over auto routing", () => {
  assert.equal(resolveAssistantWorkflow("write", "你好"), "write");
  assert.equal(resolveAssistantWorkflow("chat", "帮我写一份总结"), "chat");
});
