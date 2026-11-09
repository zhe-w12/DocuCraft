import test from "node:test";
import assert from "node:assert/strict";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createServer } from "vite";

test("shows local-save failures on the home screen and in chat when the document pane is closed", async () => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false } });
  let assistantState;
  try {
    const stateModule = await vite.ssrLoadModule("/src/assistant-state.js");
    ({ assistantState } = stateModule);
    assistantState.mode = "auto";
    assistantState.activeId = null;
    assistantState.conversations = [];
    assistantState.localSaveFailed = true;
    const { default: AssistantWorkspace } = await vite.ssrLoadModule("/src/AssistantWorkspace.vue");
    const homeHtml = await renderToString(createSSRApp(AssistantWorkspace, { apiBase: "http://127.0.0.1:5000" }));
    assert.match(homeHtml, /role="alert"[^>]*>本地保存失败/);

    assistantState.mode = "chat";
    const conversation = stateModule.createAssistantConversation("chat");
    conversation.messages.push({ id: "greeting", role: "user", content: "你好" });
    assistantState.localSaveFailed = true;
    const html = await renderToString(createSSRApp(AssistantWorkspace, { apiBase: "http://127.0.0.1:5000" }));

    assert.match(html, /role="alert"[^>]*>本地保存失败/);
    assert.ok(html.includes("请复制重要内容后再刷新页面"));
  } finally {
    if (assistantState) {
      assistantState.mode = "auto";
      assistantState.localSaveFailed = false;
    }
    await vite.close();
  }
});
