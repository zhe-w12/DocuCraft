import { reactive, toRaw } from "vue";
import { AssistantStorage } from "./assistant-storage.js";
import { createId } from "./ids.js";

const STORE_KEY = "iscan-assistant-conversations-v1";
const DOCUMENT_STORE_KEY = "iscan-assistant-document-v1";
const MEMORY_PROFILE_KEY = "docucraft-assistant-memory-profile-v1";

function readMemoryProfileId() {
  try {
    const existing = localStorage.getItem(MEMORY_PROFILE_KEY);
    if (typeof existing === "string" && /^[A-Za-z0-9_-]{20,64}$/.test(existing)) return existing;
    const profileId = createId();
    localStorage.setItem(MEMORY_PROFILE_KEY, profileId);
    return profileId;
  } catch {
    return "";
  }
}

export const assistantMemoryProfileId = readMemoryProfileId();

function readState() {
  try {
    const value = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    const conversations = Array.isArray(value.conversations)
      ? value.conversations
          .filter(item => item && typeof item.id === "string" && Array.isArray(item.messages))
      : [];
    return {
      conversations,
      activeId: conversations.some(item => item.id === value.activeId) ? value.activeId : null,
      savedAt: Number.isFinite(value.savedAt) ? value.savedAt : 0,
    };
  } catch {
    return { conversations: [], activeId: null };
  }
}

function readDocument() {
  try {
    const value = JSON.parse(localStorage.getItem(DOCUMENT_STORE_KEY) || "{}");
    return {
      title: typeof value.title === "string" ? value.title : "",
      content: typeof value.content === "string" ? value.content : "",
    };
  } catch {
    return { title: "", content: "" };
  }
}

function isDocumentReply(message) {
  if (message?.documentOutput === true) return true;
  return message?.role === "assistant" && Boolean(message.workflow) && !["auto", "chat", "plan"].includes(message.workflow);
}

function conversationForStorage(conversation) {
  const raw = toRaw(conversation);
  return {
    ...raw,
    document: { ...(raw.document || {}) },
    messages: (raw.messages || []).map(message => {
      const plainMessage = { ...toRaw(message) };
      return isDocumentReply(plainMessage) ? { ...plainMessage, content: "" } : plainMessage;
    }),
  };
}

function findLegacyDocumentOwner(conversations, document, fallback) {
  const legacyContent = String(document.content || "").replace(/\s+/g, "");
  if (legacyContent.length < 32) return fallback;
  let bestMatch = null;

  for (const conversation of conversations) {
    for (const message of conversation.messages || []) {
      if (message.role !== "assistant" || typeof message.content !== "string") continue;
      const heading = message.content.match(/^#\s+(.+)$/m)?.[1]?.trim() || "";
      const body = message.content.replace(/^#\s+.+(?:\r?\n|$)/m, "").replace(/\s+/g, "");
      if (body.length < 32) continue;
      const overlap = legacyContent.includes(body)
        ? body.length / legacyContent.length
        : body.includes(legacyContent) ? legacyContent.length / body.length : 0;
      const titleBonus = heading && heading.replace(/\s+/g, "") === String(document.title || "").replace(/\s+/g, "") ? 0.15 : 0;
      const score = overlap + titleBonus;
      if (!bestMatch || score > bestMatch.score) bestMatch = { conversation, score };
    }
  }

  return bestMatch?.score >= 0.25 ? bestMatch.conversation : fallback;
}

const saved = readState();
const savedDocument = readDocument();
const activeSavedConversation = saved.conversations.find(item => item.id === saved.activeId) || null;
const legacyDocumentOwner = findLegacyDocumentOwner(saved.conversations, savedDocument, activeSavedConversation);
let needsDocumentMigration = false;

for (const conversation of saved.conversations) {
  const document = conversation.document;
  if (document && typeof document.title === "string" && typeof document.content === "string") continue;
  conversation.document = conversation === legacyDocumentOwner
    ? { ...savedDocument }
    : { title: "", content: "" };
  needsDocumentMigration = true;
}

export const assistantState = reactive({
  conversations: saved.conversations,
  activeId: saved.activeId,
  mode: activeSavedConversation?.mode || "auto",
  documentTitle: activeSavedConversation?.document.title || "",
  documentContent: activeSavedConversation?.document.content || "",
  ready: false,
  localLoadFailed: false,
  localSaveFailed: false,
  requestActive: false,
  requestConversationId: "",
  status: {
    loading: true,
    configured: false,
    provider: "阿里云百炼",
    model: "",
    webSearchConfigured: false,
  },
});

let storageMode = typeof window !== "undefined" && globalThis.indexedDB ? "opening" : "localStorage";
let pendingSnapshot = null;
let queuedWrites = Promise.resolve();
let writeGeneration = 0;

function snapshotFromState() {
  const currentConversations = assistantState.conversations;
  const conversations = [...currentConversations]
    .sort((left, right) => (right.updatedAt || 0) - (left.updatedAt || 0));
  if (conversations.some((conversation, index) => conversation !== currentConversations[index])) {
    assistantState.conversations = conversations;
  }
  return {
    conversations: conversations.map(conversationForStorage),
    activeId: assistantState.activeId,
    savedAt: Date.now(),
  };
}

function normalizeSnapshot(value) {
  if (!value || !Array.isArray(value.conversations)) return null;
  const conversations = value.conversations
    .filter(item => item && typeof item.id === "string" && Array.isArray(item.messages))
    .map(item => ({
      ...item,
      document: item.document && typeof item.document.title === "string" && typeof item.document.content === "string"
        ? item.document
        : { title: "", content: "" },
    }));
  return {
    conversations,
    activeId: conversations.some(item => item.id === value.activeId) ? value.activeId : null,
    savedAt: Number.isFinite(value.savedAt) ? value.savedAt : 0,
  };
}

function applySnapshot(snapshot) {
  assistantState.conversations = snapshot.conversations;
  assistantState.activeId = snapshot.activeId;
  const active = snapshot.conversations.find(item => item.id === snapshot.activeId);
  assistantState.mode = active?.mode || "auto";
  assistantState.documentTitle = active?.document?.title || "";
  assistantState.documentContent = active?.document?.content || "";
}

function removeLegacySnapshots() {
  try {
    globalThis.localStorage.removeItem(STORE_KEY);
    globalThis.localStorage.removeItem(DOCUMENT_STORE_KEY);
  } catch { /* The IndexedDB copy remains authoritative if local storage is unavailable. */ }
}

function saveLocalSnapshot(snapshot) {
  try {
    globalThis.localStorage.setItem(STORE_KEY, JSON.stringify(snapshot));
    globalThis.localStorage.removeItem(DOCUMENT_STORE_KEY);
    return true;
  } catch {
    return false;
  }
}

function dispatchSnapshot(snapshot) {
  if (storageMode === "opening") {
    pendingSnapshot = snapshot;
    return true;
  }
  if (storageMode === "unavailable") return false;
  if (storageMode === "localStorage") {
    const saved = saveLocalSnapshot(snapshot);
    assistantState.localSaveFailed = !saved;
    return saved;
  }

  const generation = ++writeGeneration;
  queuedWrites = queuedWrites.catch(() => {}).then(async () => {
    try {
      await AssistantStorage.save(snapshot);
      if (generation === writeGeneration) {
        assistantState.localSaveFailed = false;
        removeLegacySnapshots();
      }
    } catch {
      const saved = saveLocalSnapshot(snapshot);
      if (generation === writeGeneration) assistantState.localSaveFailed = !saved;
    }
  });
  return true;
}

function hasLegacyData() {
  return saved.conversations.length > 0 || Boolean(savedDocument.title || savedDocument.content);
}

async function initializeAssistantStorage() {
  if (typeof window === "undefined" || !globalThis.indexedDB) {
    storageMode = "localStorage";
    assistantState.ready = true;
    if (needsDocumentMigration && hasLegacyData()) persistAssistantState();
    return;
  }

  try {
    const storedValue = await AssistantStorage.load();
    if (storedValue && !Array.isArray(storedValue.conversations)) {
      throw new Error("本地对话数据格式无法识别。");
    }
    const stored = normalizeSnapshot(storedValue);
    const local = normalizeSnapshot(saved);
    const preferLocal = hasLegacyData() && (!stored
      || local.savedAt > stored.savedAt
      || (!stored.conversations.length && hasLegacyData()));

    storageMode = "indexedDB";
    if (stored && !preferLocal) {
      applySnapshot(stored);
      removeLegacySnapshots();
    } else if (hasLegacyData()) {
      const snapshot = snapshotFromState();
      try {
        await AssistantStorage.save(snapshot);
        removeLegacySnapshots();
      } catch {
        assistantState.localSaveFailed = !saveLocalSnapshot(snapshot);
      }
    } else {
      removeLegacySnapshots();
    }
  } catch (error) {
    if (error?.name === "NotSupportedError") {
      storageMode = "localStorage";
      if (needsDocumentMigration && hasLegacyData()) {
        assistantState.localSaveFailed = !saveLocalSnapshot(snapshotFromState());
      }
    } else if (saved.savedAt > 0 || hasLegacyData()) {
      storageMode = "localStorage";
    } else {
      storageMode = "unavailable";
      assistantState.localLoadFailed = true;
    }
  } finally {
    assistantState.ready = true;
    if (pendingSnapshot && storageMode !== "unavailable") {
      const snapshot = pendingSnapshot;
      pendingSnapshot = null;
      dispatchSnapshot(snapshot);
    }
  }
}

export function persistAssistantDocument() {
  const conversation = assistantState.conversations.find(item => item.id === assistantState.activeId);
  if (!conversation) return false;
  conversation.document = {
    title: assistantState.documentTitle,
    content: assistantState.documentContent,
  };
  return persistAssistantState();
}

export function getAssistantConversationDraft(id) {
  const conversation = assistantState.conversations.find(item => item.id === id);
  return typeof conversation?.draft === "string" ? conversation.draft : "";
}

export function setAssistantConversationDraft(id, value) {
  const conversation = assistantState.conversations.find(item => item.id === id);
  if (!conversation) return null;
  conversation.draft = String(value || "");
  return conversation;
}

export function persistAssistantState() {
  try {
    if (assistantState.localLoadFailed || storageMode === "unavailable") return false;
    return dispatchSnapshot(snapshotFromState());
  } catch {
    assistantState.localSaveFailed = true;
    return false;
  }
}

export function flushAssistantPersistence() {
  return queuedWrites.then(() => !assistantState.localSaveFailed);
}

export function buildAssistantRequestMessages(conversation) {
  const messages = conversation?.messages || [];
  let latestDocumentReply = -1;
  messages.forEach((message, index) => {
    if (isDocumentReply(message)) latestDocumentReply = index;
  });

  const document = conversation?.document || {};
  const title = document.title === "正在生成文档…" ? "" : String(document.title || "").trim();
  const content = String(document.content || "").trim();
  const currentDocument = [title ? `# ${title}` : "", content].filter(Boolean).join("\n\n");

  return messages.map((message, index) => {
    if (!isDocumentReply(message)) return { role: message.role, content: message.content };
    return {
      role: message.role,
      content: index === latestDocumentReply ? currentDocument || message.content : "",
    };
  });
}

export function createAssistantConversation(mode = "auto") {
  persistAssistantDocument();
  const preserveOrphanedLegacyDocument = !assistantState.activeId && !assistantState.conversations.length;
  const document = preserveOrphanedLegacyDocument
    ? { ...savedDocument }
    : { title: "", content: "" };
  const now = Date.now();
  const conversation = {
    id: createId(),
    title: "新对话",
    mode,
    draft: "",
    createdAt: now,
    updatedAt: now,
    messages: [],
    document,
  };
  assistantState.conversations.unshift(conversation);
  assistantState.activeId = conversation.id;
  assistantState.mode = mode;
  assistantState.documentTitle = document.title;
  assistantState.documentContent = document.content;
  persistAssistantState();
  return conversation;
}

export function selectAssistantConversation(id) {
  const nextConversation = assistantState.conversations.find(item => item.id === id);
  if (!nextConversation) return false;
  persistAssistantDocument();
  assistantState.activeId = id;
  assistantState.mode = nextConversation.mode || "auto";
  assistantState.documentTitle = nextConversation.document?.title || "";
  assistantState.documentContent = nextConversation.document?.content || "";
  persistAssistantState();
  return true;
}

export function isConversationGenerating(id) {
  return Boolean(id && assistantState.requestActive && assistantState.requestConversationId === id);
}

export function removeAssistantConversation(id) {
  if (isConversationGenerating(id)) return false;
  persistAssistantDocument();
  assistantState.conversations = assistantState.conversations.filter(item => item.id !== id);
  if (assistantState.activeId === id) {
    const nextConversation = assistantState.conversations[0] || null;
    assistantState.activeId = nextConversation?.id || null;
    assistantState.mode = nextConversation?.mode || "auto";
    assistantState.documentTitle = nextConversation?.document?.title || "";
    assistantState.documentContent = nextConversation?.document?.content || "";
  }
  persistAssistantState();
  return true;
}

void initializeAssistantStorage();
