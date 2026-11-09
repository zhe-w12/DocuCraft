import assert from "node:assert/strict";
import test from "node:test";
import { cleanActiveDocument } from "../src/document-chrome.js";

test("cleans restored document metadata in both the active state and its conversation", () => {
  const conversation = { document: { title: "旧标题", content: "旧内容" } };
  const state = {
    documentTitle: "事务锁知识点文档",
    documentContent: "前言\n\n你\n\n我的工作区\n\n自动保存\n\n## 第一节\n正文",
  };

  assert.equal(cleanActiveDocument(state, conversation), true);
  assert.equal(state.documentContent, "前言\n\n## 第一节\n正文");
  assert.deepEqual(conversation.document, { title: "事务锁知识点文档", content: "前言\n\n## 第一节\n正文" });
});

test("does not rewrite a document when no faux metadata row exists", () => {
  const conversation = { document: { title: "原文", content: "原正文" } };
  const state = { documentTitle: "原文", documentContent: "正文提到我的工作区和自动保存功能。" };

  assert.equal(cleanActiveDocument(state, conversation), false);
  assert.deepEqual(conversation.document, { title: "原文", content: "原正文" });
});
