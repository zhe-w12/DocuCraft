import assert from "node:assert/strict";
import test from "node:test";

test("each assistant conversation keeps its own document, including legacy migration", async () => {
  const storage = new Map([
    ["iscan-assistant-conversations-v1", JSON.stringify({
      activeId: "hello-chat",
      conversations: [
        {
          id: "hello-chat",
          title: "你好",
          mode: "auto",
          messages: [
            { role: "user", content: "你好" },
            { role: "assistant", workflow: "auto", content: "你好！我可以帮你写作或修改文档。" },
          ],
        },
        {
          id: "math-chat",
          title: "微积分",
          mode: "auto",
          messages: [
            { role: "user", content: "生成微积分知识点" },
            { role: "assistant", workflow: "auto", content: "# 微积分知识点\n旧版本当前对话的微积分文档，包含函数定义、常见函数类型、函数性质以及相关极限知识点和典型公式内容。" },
          ],
        },
      ],
    })],
    ["iscan-assistant-document-v1", JSON.stringify({
      title: "微积分知识点",
      content: "旧版本当前对话的微积分文档，包含函数定义、常见函数类型、函数性质以及相关极限知识点和典型公式内容。",
    })],
  ]);
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?isolated-document-test");
  assert.equal(state.assistantState.ready, true);
  assert.equal(state.assistantState.documentTitle, "");
  assert.equal(state.assistantState.conversations.find(item => item.id === "math-chat").document.title, "微积分知识点");
  assert.equal(state.assistantState.conversations.find(item => item.id === "hello-chat").document.content, "");
  const migratedStorage = JSON.parse(storage.get("iscan-assistant-conversations-v1"));
  assert.equal(
    migratedStorage.conversations.find(item => item.id === "math-chat").messages[1].content,
    "# 微积分知识点\n旧版本当前对话的微积分文档，包含函数定义、常见函数类型、函数性质以及相关极限知识点和典型公式内容。",
  );

  state.selectAssistantConversation("hello-chat");
  assert.equal(state.assistantState.documentTitle, "");
  state.assistantState.documentTitle = "你好文档";
  state.assistantState.documentContent = "这是第一段对话自己的文档";
  state.persistAssistantDocument();

  state.selectAssistantConversation("math-chat");
  assert.equal(state.assistantState.documentTitle, "微积分知识点");
  assert.equal(state.assistantState.documentContent, "旧版本当前对话的微积分文档，包含函数定义、常见函数类型、函数性质以及相关极限知识点和典型公式内容。");

  const fresh = state.createAssistantConversation("auto");
  assert.equal(state.assistantState.documentTitle, "");
  assert.equal(state.assistantState.documentContent, "");
  state.selectAssistantConversation("hello-chat");
  assert.equal(state.assistantState.documentTitle, "你好文档");
  assert.equal(state.assistantState.documentContent, "这是第一段对话自己的文档");
  assert.ok(fresh.document);
  assert.equal(storage.has("iscan-assistant-document-v1"), false);
});

test("a standalone legacy document is retained when the first conversation is created", async () => {
  const storage = new Map([
    ["iscan-assistant-document-v1", JSON.stringify({ title: "旧文档", content: "仍需保留的本地内容" })],
  ]);
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?orphan-document-test");
  const conversation = state.createAssistantConversation("auto");
  assert.equal(conversation.document.title, "旧文档");
  assert.equal(state.assistantState.documentContent, "仍需保留的本地内容");
  assert.equal(storage.has("iscan-assistant-document-v1"), false);
});

test("falls back to legacy local data when IndexedDB cannot be opened", async t => {
  const storage = new Map([
    ["iscan-assistant-conversations-v1", JSON.stringify({
      activeId: "legacy-chat",
      conversations: [{ id: "legacy-chat", title: "旧对话", messages: [{ role: "user", content: "保留我" }] }],
    })],
  ]);
  const previousIndexedDB = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  t.after(() => {
    if (previousIndexedDB) Object.defineProperty(globalThis, "indexedDB", previousIndexedDB);
    else delete globalThis.indexedDB;
  });
  Object.defineProperty(globalThis, "indexedDB", {
    configurable: true,
    value: { open() { throw new DOMException("blocked", "SecurityError"); } },
  });
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?indexeddb-fallback-test");
  while (!state.assistantState.ready) await new Promise(resolve => setImmediate(resolve));

  assert.equal(state.assistantState.localLoadFailed, false);
  assert.equal(state.assistantState.activeId, "legacy-chat");
  assert.equal(state.persistAssistantState(), true);
  assert.equal(JSON.parse(storage.get("iscan-assistant-conversations-v1")).savedAt > 0, true);
});

test("keeps every conversation instead of silently evicting older chats", async () => {
  const storage = new Map();
  const conversations = Array.from({ length: 15 }, (_, index) => ({
    id: `chat-${index}`,
    title: `对话 ${index}`,
    updatedAt: index,
    messages: [{ role: "user", content: `问题 ${index}` }],
    document: { title: "", content: "" },
  }));
  storage.set("iscan-assistant-conversations-v1", JSON.stringify({
    activeId: "chat-14",
    conversations,
  }));
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?unbounded-conversation-retention-test");
  assert.equal(state.assistantState.conversations.length, 15);
  assert.equal(state.assistantState.activeId, "chat-14");
  assert.equal(state.persistAssistantState(), true);
  assert.equal(JSON.parse(storage.get("iscan-assistant-conversations-v1")).conversations.length, 15);
  state.createAssistantConversation("auto");
  assert.equal(state.assistantState.conversations.length, 16);
  assert.equal(JSON.parse(storage.get("iscan-assistant-conversations-v1")).conversations.length, 16);
});

test("keeps the reactive conversation array when snapshot sorting does not change its order", async () => {
  const storage = new Map([[
    "iscan-assistant-conversations-v1",
    JSON.stringify({
      activeId: "newer",
      conversations: [
        { id: "older", updatedAt: 1, messages: [{ role: "user", content: "旧对话" }], document: { title: "", content: "" } },
        { id: "newer", updatedAt: 2, messages: [{ role: "user", content: "新对话" }], document: { title: "", content: "" } },
      ],
    }),
  ]]);
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?stable-conversation-order-test");
  assert.equal(state.persistAssistantState(), true);
  assert.deepEqual(state.assistantState.conversations.map(item => item.id), ["newer", "older"]);
  const sortedConversations = state.assistantState.conversations;

  assert.equal(state.persistAssistantState(), true);
  assert.strictEqual(state.assistantState.conversations, sortedConversations);
});

test("protects the conversation with an active generation from deletion", async () => {
  const storage = new Map();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?active-generation-delete-guard-test");
  const generating = state.createAssistantConversation("auto");
  const other = state.createAssistantConversation("auto");
  state.assistantState.requestActive = true;
  state.assistantState.requestConversationId = generating.id;

  assert.equal(state.removeAssistantConversation(generating.id), false);
  assert.ok(state.assistantState.conversations.some(item => item.id === generating.id));
  assert.equal(state.removeAssistantConversation(other.id), true);
  assert.ok(state.assistantState.conversations.some(item => item.id === generating.id));

  state.assistantState.requestActive = false;
  state.assistantState.requestConversationId = "";
  assert.equal(state.removeAssistantConversation(generating.id), true);
  assert.equal(state.assistantState.conversations.length, 0);
});

test("keeps unsent drafts isolated and persisted per conversation", async () => {
  const storage = new Map();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?isolated-draft-persistence-test");
  const first = state.createAssistantConversation("auto");
  state.setAssistantConversationDraft(first.id, "第一条对话尚未发送的草稿");
  state.persistAssistantState();

  const second = state.createAssistantConversation("auto");
  state.setAssistantConversationDraft(second.id, "第二条对话的另一份草稿");
  state.persistAssistantState();

  state.selectAssistantConversation(first.id);
  assert.equal(state.getAssistantConversationDraft(first.id), "第一条对话尚未发送的草稿");
  assert.equal(state.getAssistantConversationDraft(second.id), "第二条对话的另一份草稿");

  const saved = JSON.parse(storage.get("iscan-assistant-conversations-v1"));
  assert.equal(saved.conversations.find(item => item.id === first.id).draft, "第一条对话尚未发送的草稿");
  assert.equal(saved.conversations.find(item => item.id === second.id).draft, "第二条对话的另一份草稿");

  const restored = await import("../src/assistant-state.js?isolated-draft-restoration-test");
  assert.equal(restored.getAssistantConversationDraft(first.id), "第一条对话尚未发送的草稿");
  assert.equal(restored.getAssistantConversationDraft(second.id), "第二条对话的另一份草稿");
});

test("reports local persistence failures and clears the warning when storage recovers", async () => {
  let failWrites = true;
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: () => null,
      setItem() {
        if (failWrites) throw new Error("storage full");
      },
      removeItem() {},
    },
  });

  const state = await import("../src/assistant-state.js?storage-failure-test");
  assert.equal(state.persistAssistantState(), false);
  assert.equal(state.assistantState.localSaveFailed, true);

  failWrites = false;
  assert.equal(state.persistAssistantState(), true);
  assert.equal(state.assistantState.localSaveFailed, false);
});

test("persists a document-workflow reply once while keeping the editable document", async () => {
  const storage = new Map();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  });

  const state = await import("../src/assistant-state.js?deduplicated-storage-test");
  const conversation = state.createAssistantConversation("write");
  conversation.messages.push(
    { role: "user", content: "写一份方案" },
    { role: "assistant", workflow: "write", status: "complete", content: "# 项目方案\n\n方案正文" },
    { role: "assistant", workflow: "auto", documentOutput: true, status: "complete", content: "# 项目方案\n\n第二轮完整正文" },
    { role: "assistant", workflow: "auto", status: "complete", content: "旧版尚未分类的回复" },
    { role: "assistant", workflow: "chat", status: "complete", content: "好的，我可以继续帮你调整。" },
  );
  state.assistantState.documentTitle = "项目方案";
  state.assistantState.documentContent = "第二轮完整正文";
  state.persistAssistantDocument();

  const saved = JSON.parse(storage.get("iscan-assistant-conversations-v1"));
  const persisted = saved.conversations[0];
  assert.equal(persisted.document.content, "第二轮完整正文");
  assert.equal(persisted.messages[1].content, "");
  assert.equal(persisted.messages[2].content, "");
  assert.equal(persisted.messages[3].content, "旧版尚未分类的回复");
  assert.equal(persisted.messages[4].content, "好的，我可以继续帮你调整。");
});

test("rebuilds model context with the latest editable document instead of old document copies", async () => {
  const { buildAssistantRequestMessages } = await import("../src/assistant-state.js?request-context-test");
  const messages = buildAssistantRequestMessages({
    document: { title: "修订稿", content: "现在的完整正文" },
    messages: [
      { role: "user", content: "先写一版" },
      { role: "assistant", workflow: "auto", documentOutput: true, content: "旧版长文" },
      { role: "user", content: "按意见修改" },
      { role: "assistant", workflow: "revise", content: "新版长文" },
      { role: "assistant", workflow: "chat", content: "闲聊回复" },
    ],
  });

  assert.deepEqual(messages, [
    { role: "user", content: "先写一版" },
    { role: "assistant", content: "" },
    { role: "user", content: "按意见修改" },
    { role: "assistant", content: "# 修订稿\n\n现在的完整正文" },
    { role: "assistant", content: "闲聊回复" },
  ]);
});
