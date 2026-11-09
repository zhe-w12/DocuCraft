<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import MarkdownIt from "markdown-it";
import { assistantState, buildAssistantRequestMessages, createAssistantConversation, flushAssistantPersistence, getAssistantConversationDraft, persistAssistantDocument, persistAssistantState, selectAssistantConversation, setAssistantConversationDraft } from "./assistant-state.js";
import { handleAssistantComposerKeydown, resolveAssistantDraftOnConversationChange } from "./assistant-composer.js";
import { resolveAssistantWorkflow } from "./assistant-workflow.js";
import { latestAssistantMessage, latestDocumentMessageId, shouldOpenDocumentPane } from "./assistant-layout.js";
import { containsMathSource, normalizeDisplayMathDelimiters, stripDocumentChrome } from "./markdown-rendering.js";
import { cleanActiveDocument } from "./document-chrome.js";
import { createId } from "./ids.js";
import { serializeEditableDocument } from "./rich-editor.js";
import { responseErrorMessage } from "./http-errors.js";
import { isNearScrollEnd, restoreScrollPosition, scrollAdjustmentForAnchor } from "./scroll-follow.js";
import { createFrameScheduler } from "./frame-scheduler.js";
import { INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT, nextAssistantMessageHistoryLimit, sliceLatestAssistantMessages } from "./assistant-message-history.js";
import { createStreamPersistenceScheduler } from "./stream-persistence.js";
import { createStreamRenderScheduler, DOCUMENT_STREAM_RENDER_DELAY_MS } from "./stream-render.js";
import { hasSameDocumentOutline, outlineEditableDocument } from "./document-outline.js";
import { findLastItemAtOrBefore } from "./outline-position.js";
import { resolveAssistantMessageStatus } from "./assistant-message-status.js";
import { createAssistantMessageMarkdownRenderer } from "./assistant-message-markdown.js";
import { clipboardContainsFiles, insertSanitizedClipboardContent } from "./editable-paste.js";
import { copyTextToClipboard } from "./clipboard.js";
import { safeDownloadStem } from "./download-filename.js";
import { downloadBlob } from "./browser-download.js";
import ManualCopyDialog from "./ManualCopyDialog.vue";

const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true });
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);
const EMPTY_RENDERED_DOCUMENT = { outline: [], html: "" };
const mathRendererLoaded = ref(false);
let mathRendererPromise = null;

function ensureMathRenderer() {
  if (mathRendererLoaded.value) return Promise.resolve();
  if (!mathRendererPromise) {
    mathRendererPromise = Promise.all([
      import("markdown-it-texmath"),
      import("katex"),
      import("katex/dist/katex.min.css"),
      import("markdown-it-texmath/css/texmath.css"),
    ]).then(([texmathModule, katexModule]) => {
      markdown.use(texmathModule.default || texmathModule, {
        engine: katexModule.default,
        delimiters: ["dollars", "brackets"],
        katexOptions: { throwOnError: false },
      });
      mathRendererLoaded.value = true;
    }).catch(error => {
      mathRendererPromise = null;
      throw error;
    });
  }
  return mathRendererPromise;
}

function requestMathRenderer(content) {
  if (containsMathSource(content) && !mathRendererLoaded.value) {
    void ensureMathRenderer().catch(() => {});
  }
}

const renderAssistantMessage = createAssistantMessageMarkdownRenderer({
  render: content => markdown.render(normalizeDisplayMathDelimiters(content)),
  requestMathRenderer,
  isRendererReady: () => mathRendererLoaded.value,
});

const props = defineProps({
  apiBase: { type: String, required: true },
  active: { type: Boolean, default: true },
});
const logoUrl = `${import.meta.env.BASE_URL}iscan-logo.png`;
const SAVE_FAILURE_TEXT = "本地保存失败；此设备上的对话或文档可能未保存。请复制重要内容后再刷新页面。";
let statusRequestSequence = 0;

const MODE_OPTIONS = [
  { id: "auto", label: "自动判断", hint: "先识别你要从零写作还是修改已有内容。" },
  { id: "write", label: "从零写作", hint: "告诉我主题、用途和读者；我会先组织结构，再写初稿。" },
  { id: "revise", label: "修改原文", hint: "粘贴原文并说明修改要求；我会保留原有事实和核心观点。" },
  { id: "polish", label: "润色", hint: "改善流畅度和表达，保留原文信息。" },
  { id: "shorten", label: "缩写", hint: "压缩篇幅，保留关键事实和结论。" },
  { id: "expand", label: "扩写", hint: "补足结构和解释，不编造依据不足的细节。" },
  { id: "rewrite", label: "改写", hint: "按你指定的语气重写，保持事实与观点不变。" },
  { id: "summarize", label: "总结", hint: "提炼你提供材料中的主题、要点和结论。" },
  { id: "chat", label: "闲聊", hint: "轻松聊聊，像普通对话一样自然接话，不强行生成文档。" },
];

const starters = [
  {
    icon: "✦",
    title: "从零开始写作",
    description: "公文、方案、简历、文章或社交媒体文案",
    mode: "write",
    prompt: "请帮我写一份内容。我会补充主题、用途、读者和希望的语气。",
  },
  {
    icon: "↗",
    title: "润色一段文字",
    description: "保留原意，让表达更自然、更清楚",
    mode: "polish",
    prompt: "请润色下面这段文字，保留事实和核心观点，让语言更自然：\n\n",
  },
  {
    icon: "≋",
    title: "总结一份材料",
    description: "粘贴内容，提取重点和结论",
    mode: "summarize",
    prompt: "请总结下面的材料，列出主题、关键要点和结论：\n\n",
  },
  {
    icon: "☕",
    title: "轻松聊聊天",
    description: "聊聊日常、兴趣或脑海里的想法",
    mode: "chat",
    prompt: "嗨，陪我轻松聊一会儿吧。",
  },
];

const draft = ref("");
const sending = ref(false);
const currentRequestWorkflow = ref("");
const currentRequestConversationId = ref("");
const streamingMessageId = ref("");
const documentPageScrollElement = ref(null);
const followDocumentScroll = ref(true);
const documentContentEditor = ref(null);
const documentEditorHtml = ref("");
const documentEditorDirty = ref(false);
const inlineError = ref("");
const messageList = ref(null);
const followMessageScroll = ref(true);
const visibleMessageCount = ref(INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT);
const composer = ref(null);
const documentPaneElement = ref(null);
const assistantPanelElement = ref(null);
const mobilePane = ref("assistant");
const panelsSwapped = ref(false);
const fullscreenTarget = ref("");
const exportMenuOpen = ref(false);
const editorMenuOpen = ref(false);
const exportMenuTrigger = ref(null);
const editorMenuTrigger = ref(null);
const editorMode = ref("read");
const outlineCollapsed = ref(false);
const activeOutlineId = ref("");
const manualCopyDialog = ref(null);
let streamMessageScrollFrame = 0;
let streamDocumentScrollFrame = 0;
let inactiveMessageScrollTop = 0;
let inactiveDocumentScrollTop = 0;
let draftConversationId = "";
const streamPersistence = createStreamPersistenceScheduler(conversation => {
  if (assistantState.activeId === conversation.id) persistAssistantDocument();
  else persistAssistantState();
});
const documentPersistence = createStreamPersistenceScheduler(() => persistAssistantDocument(), 500);
const activeConversation = computed(() => assistantState.conversations.find(item => item.id === assistantState.activeId) || null);
const requestConversation = computed(() => assistantState.conversations.find(item => item.id === assistantState.requestConversationId) || null);
const requestBelongsToActiveConversation = computed(() => Boolean(
  sending.value && assistantState.requestActive && assistantState.requestConversationId === assistantState.activeId,
));
const requestIsInAnotherConversation = computed(() => Boolean(
  sending.value && assistantState.requestActive && assistantState.requestConversationId !== assistantState.activeId,
));
const activeMessages = computed(() => activeConversation.value?.messages || []);
const visibleMessages = computed(() => sliceLatestAssistantMessages(activeMessages.value, visibleMessageCount.value));
const hasOlderMessages = computed(() => visibleMessages.value.length < activeMessages.value.length);
const latestDocumentId = computed(() => latestDocumentMessageId(activeMessages.value));
const selectedMode = computed(() => MODE_OPTIONS.find(option => option.id === assistantState.mode) || MODE_OPTIONS[0]);
const lastAssistantMessage = computed(() => latestAssistantMessage(activeMessages.value));
const documentPaneOpen = ref(shouldOpenDocumentPane(assistantState.mode, lastAssistantMessage.value?.workflow));
const isChatLayout = computed(() => assistantState.mode === "chat" || (
  assistantState.mode === "auto" && (
    sending.value && currentRequestConversationId.value === assistantState.activeId
      ? currentRequestWorkflow.value
      : lastAssistantMessage.value?.workflow
  ) === "chat"
));
const renderedDocument = computed(() => {
  if (!props.active) return EMPTY_RENDERED_DOCUMENT;
  const content = stripDocumentChrome(assistantState.documentContent || "");
  requestMathRenderer(content);
  const tokens = markdown.parse(normalizeDisplayMathDelimiters(content), {});
  const outline = [];
  tokens.forEach((token, index) => {
    if (token.type !== "heading_open") return;
    const inline = tokens[index + 1];
    const title = (inline?.children || [])
      .filter(child => ["text", "code_inline", "html_inline"].includes(child.type))
      .map(child => child.content)
      .join("")
      .replace(/\s+/g, " ")
      .trim();
    const id = `iscan-document-heading-${outline.length + 1}`;
    token.attrSet("id", id);
    outline.push({ id, title: title || "未命名标题", level: Number(token.tag.slice(1)), line: token.map?.[0] || 0 });
  });
  return { outline, html: markdown.renderer.render(tokens, markdown.options, {}) };
});
const editableDocumentOutline = ref([]);
const documentOutline = computed(() => (
  editorMode.value === "edit" ? editableDocumentOutline.value : renderedDocument.value.outline
));
const activeOutlineFrame = createFrameScheduler(updateActiveOutline);

if (activeConversation.value?.mode) {
  assistantState.mode = activeConversation.value.mode;
}

function migrateLegacyAutoChat(conversation) {
  if (!conversation) return;
  let lastUserMessage = null;
  let reclassifiedMessage = null;
  let changed = false;
  for (const message of conversation.messages || []) {
    if (message.role === "user") lastUserMessage = message;
    if (
      message.role === "assistant" && message.content &&
      (message.documentOutput === true || (message.workflow && !["auto", "chat"].includes(message.workflow)))
    ) changed = true;
    if (message.role === "assistant" && message.workflow === "auto" && lastUserMessage) {
      const resolved = resolveAssistantWorkflow("auto", lastUserMessage.content, conversation.messages.slice(0, conversation.messages.indexOf(message)));
      if (resolved === "chat") {
        message.workflow = "chat";
        reclassifiedMessage = { prompt: lastUserMessage.content, message };
        changed = true;
      } else if (message.documentOutput !== true) {
        message.documentOutput = true;
        changed = true;
      }
    }
  }
  if (!changed) return;

  // Remove only a legacy greeting response that this app itself had copied verbatim into the document pane.
  if (reclassifiedMessage &&
    assistantState.documentTitle.trim() === reclassifiedMessage.prompt.trim() &&
    assistantState.documentContent.trim() === reclassifiedMessage.message.content.trim()
  ) {
    assistantState.documentTitle = "";
    assistantState.documentContent = "";
    persistAssistantDocument();
  }
  persistAssistantState();
}

documentPaneOpen.value = shouldOpenDocumentPane(assistantState.mode, lastAssistantMessage.value?.workflow);

function requestUrl(path) {
  return `${String(props.apiBase || "").trim().replace(/\/+$/, "")}${path}`;
}

async function refreshStatus() {
  const sequence = ++statusRequestSequence;
  assistantState.status.loading = true;
  const controller = new AbortController();
  const timer = globalThis.setTimeout(() => controller.abort(), 7000);
  try {
    const response = await fetch(requestUrl("/api/v1/assistant/status"), { signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "模型状态暂时无法读取");
    if (sequence !== statusRequestSequence) return;
    Object.assign(assistantState.status, {
      loading: false,
      configured: Boolean(data.configured),
      provider: data.provider || "AI 模型",
      model: data.model || "",
      error: "",
    });
  } catch (error) {
    if (sequence !== statusRequestSequence) return;
    Object.assign(assistantState.status, {
      loading: false,
      configured: false,
      provider: "AI 模型",
      model: "",
      error: error?.name === "AbortError" ? "连接超时，请重新检测" : "连接不到 AI 服务",
    });
  } finally {
    globalThis.clearTimeout(timer);
  }
}

function createConversation(mode = assistantState.mode) {
  const previousDraft = resolveAssistantDraftOnConversationChange({
    previousConversationId: assistantState.activeId,
    draftConversationId,
    currentDraft: draft.value,
    nextDraft: "",
  });
  const conversation = createAssistantConversation(mode);
  draftConversationId = conversation.id;
  if (previousDraft.preserveUnassignedDraft && previousDraft.value) {
    setAssistantConversationDraft(conversation.id, previousDraft.value);
    persistAssistantState();
  }
  draft.value = previousDraft.value;
  inlineError.value = "";
  nextTick(() => composer.value?.focus());
  return conversation;
}

function openRequestConversation() {
  if (!assistantState.requestActive || !assistantState.requestConversationId) return;
  selectAssistantConversation(assistantState.requestConversationId);
}

function chooseStarter(starter) {
  if (!activeConversation.value) createConversation(starter.mode);
  assistantState.mode = starter.mode;
  if (activeConversation.value) activeConversation.value.mode = starter.mode;
  draft.value = starter.prompt;
  nextTick(() => {
    if (!composer.value) return;
    composer.value.focus();
    resizeComposer();
  });
}

function persistDocument() {
  persistAssistantDocument();
}

function scheduleDocumentPersistence() {
  if (activeConversation.value) {
    activeConversation.value.document = {
      title: assistantState.documentTitle,
      content: assistantState.documentContent,
    };
  }
  documentPersistence.schedule();
}

function flushPendingPersistence() {
  documentPersistence.flush();
  flushStreamPersistence();
}

function cleanLoadedDocument() {
  if (cleanActiveDocument(assistantState, activeConversation.value)) persistAssistantDocument();
}

function updateConversationDocument(conversation, title, content, { persist = true, scroll = true, preserveScrollPosition = false } = {}) {
  content = stripDocumentChrome(content);
  conversation.document = { title, content };
  if (assistantState.activeId === conversation.id) {
    assistantState.documentTitle = title;
    assistantState.documentContent = content;
    if (persist) persistAssistantDocument();
    if (scroll) scrollDocumentToLatest(preserveScrollPosition);
    return;
  }
  if (persist) persistAssistantState();
}

function assistantMessageStatus(message) {
  return resolveAssistantMessageStatus(message, {
    sending: sending.value,
    activeConversationId: assistantState.activeId,
    requestConversationId: currentRequestConversationId.value,
    streamingMessageId: streamingMessageId.value,
  });
}

function documentTaskStatus(message) {
  const status = assistantMessageStatus(message);
  if (status === "streaming") {
    return {
      title: "正在生成文档",
      detail: "内容正在持续写入左侧文档，生成完成后可以直接编辑或继续提出修改要求。",
      icon: "loading",
    };
  }
  if (status === "complete") {
    return {
      title: "文档已生成完成",
      detail: "完整内容已写入左侧文档。你可以手动编辑，或继续告诉我需要调整的部分。",
      icon: "complete",
    };
  }
  return {
    title: "本轮生成已中断",
    detail: "已生成的内容保留在左侧文档里，可以继续让 AI 补写。",
    icon: "interrupted",
  };
}

function splitDocumentTitle(content) {
  const text = stripDocumentChrome(content);
  const lines = text.split(/\r?\n/);
  let offset = 0;
  let activeFence = null;
  for (const line of lines) {
    const lineEndingLength = text.startsWith("\r\n", offset + line.length)
      ? 2
      : text[offset + line.length] === "\n" ? 1 : 0;
    const fence = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
    if (activeFence) {
      const closingFence = /^\s{0,3}(`+|~+)\s*$/.exec(line);
      if (
        closingFence &&
        closingFence[1][0] === activeFence.character &&
        closingFence[1].length >= activeFence.length
      ) {
        activeFence = null;
      }
    } else if (fence) {
      activeFence = {
        character: fence[1][0],
        length: fence[1].length,
      };
    } else {
      const heading = /^\s{0,3}#\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) {
        const title = heading[1].replace(/[*_~`]/g, "").trim();
        const before = text.slice(0, offset).trimEnd();
        const after = text.slice(offset + line.length + lineEndingLength).trimStart();
        return { title, content: [before, after].filter(Boolean).join("\n\n") };
      }
    }
    offset += line.length + lineEndingLength;
  }

  if (/^#\s*[^\r\n]*$/.test(text)) return { title: "", content: "" };
  return { title: "", content: text };
}

function fallbackDocumentTitle(prompt) {
  const title = String(prompt || "").replace(/\s+/g, " ").trim();
  return title.length > 24 ? `${title.slice(0, 24)}…` : title || "新建文档";
}

function scrollDocumentToLatest(preserveScrollPosition = false) {
  if (!props.active) return;
  if (preserveScrollPosition && !followDocumentScroll.value) return;
  nextTick(() => {
    if (!props.active) return;
    if (preserveScrollPosition && !followDocumentScroll.value) return;
    const element = documentPageScrollElement.value;
    if (element) {
      element.scrollTop = element.scrollHeight;
      followDocumentScroll.value = true;
      updateActiveOutline();
    }
  });
}

function onDocumentScroll() {
  if (!props.active) return;
  followDocumentScroll.value = isNearScrollEnd(documentPageScrollElement.value);
  activeOutlineFrame.schedule();
}

function onMessageScroll() {
  if (!props.active) return;
  followMessageScroll.value = isNearScrollEnd(messageList.value);
}

function loadOlderMessages() {
  const element = messageList.value;
  if (!element || !hasOlderMessages.value) return;

  const viewportTop = element.getBoundingClientRect().top;
  const anchor = [...element.querySelectorAll(".assistant-message")]
    .find(message => message.getBoundingClientRect().bottom > viewportTop);
  const anchorTop = anchor?.getBoundingClientRect().top;
  visibleMessageCount.value = nextAssistantMessageHistoryLimit(
    visibleMessageCount.value,
    activeMessages.value.length,
  );

  nextTick(() => {
    if (!element.isConnected) return;
    if (anchor?.isConnected && Number.isFinite(anchorTop)) {
      const adjustment = scrollAdjustmentForAnchor(anchorTop, anchor.getBoundingClientRect().top);
      if (adjustment) {
        const previousBehavior = element.style.scrollBehavior;
        element.style.scrollBehavior = "auto";
        element.scrollTop += adjustment;
        element.style.scrollBehavior = previousBehavior;
      }
    }
    followMessageScroll.value = isNearScrollEnd(element);
  });
}

function updateActiveOutline() {
  if (!props.active) return;
  const outline = documentOutline.value;
  if (!outline.length) {
    activeOutlineId.value = "";
    return;
  }

  const scroller = documentPageScrollElement.value;
  if (!scroller) return;
  const threshold = scroller.getBoundingClientRect().top + Math.min(125, scroller.clientHeight * 0.24);
  const current = findLastItemAtOrBefore(outline, threshold, item => {
    const heading = window.document.getElementById(item.id);
    return heading && scroller.contains(heading) ? heading.getBoundingClientRect().top : null;
  }) || outline[0];
  activeOutlineId.value = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2
    ? outline[outline.length - 1].id
    : current.id;
}

function scrollDocumentToStart() {
  documentPageScrollElement.value?.scrollTo({ top: 0, behavior: "smooth" });
  activeOutlineId.value = documentOutline.value[0]?.id || "";
}

function navigateToOutline(item) {
  activeOutlineId.value = item.id;
  nextTick(() => {
    const scroller = documentPageScrollElement.value;
    const heading = window.document.getElementById(item.id);
    if (!scroller || !heading || !scroller.contains(heading)) return;
    const top = scroller.scrollTop + heading.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 20;
    scroller.scrollTo({ top, behavior: "smooth" });
  });
  if (window.matchMedia("(max-width: 760px)").matches) outlineCollapsed.value = true;
}

function refreshDocumentEditor() {
  documentEditorDirty.value = false;
  const renderForEditing = () => {
    const rendered = renderedDocument.value;
    editableDocumentOutline.value = rendered.outline;
    return rendered.html.replace(/<(section|eq)(?=[\s>])/g, '<$1 contenteditable="false"');
  };
  documentEditorHtml.value = renderForEditing();
  if (containsMathSource(assistantState.documentContent)) {
    void ensureMathRenderer().then(() => {
      if (props.active && editorMode.value === "edit" && !documentEditorDirty.value) {
        documentEditorHtml.value = renderForEditing();
      }
    }).catch(() => {});
  }
}

function setEditorMode(mode) {
  closeToolbarDisclosure("editor", mode !== "edit");
  if (mode === "edit") {
    if (editorMode.value !== "edit") {
      editorMode.value = "edit";
      refreshDocumentEditor();
    }
    nextTick(() => documentContentEditor.value?.focus());
    return;
  }
  documentPersistence.flush();
  editorMode.value = "read";
  documentEditorDirty.value = false;
}

function toggleToolbarDisclosure(kind) {
  if (kind === "editor") {
    editorMenuOpen.value = !editorMenuOpen.value;
    exportMenuOpen.value = false;
    return;
  }
  exportMenuOpen.value = !exportMenuOpen.value;
  editorMenuOpen.value = false;
}

function closeToolbarDisclosure(kind, returnFocus = false) {
  const isEditor = kind === "editor";
  if (isEditor) editorMenuOpen.value = false;
  else exportMenuOpen.value = false;
  if (returnFocus) {
    const trigger = isEditor ? editorMenuTrigger : exportMenuTrigger;
    nextTick(() => trigger.value?.focus());
  }
}

function syncDocumentEditorHeadingIds() {
  if (!documentContentEditor.value) return;
  const outline = outlineEditableDocument(documentContentEditor.value);
  if (!hasSameDocumentOutline(editableDocumentOutline.value, outline)) editableDocumentOutline.value = outline;
}

function saveDocumentEditor() {
  if (!documentContentEditor.value) return;
  assistantState.documentContent = stripDocumentChrome(serializeEditableDocument(documentContentEditor.value));
  documentEditorDirty.value = true;
  scheduleDocumentPersistence();
  syncDocumentEditorHeadingIds();
}

function pasteDocumentEditor(event) {
  const clipboard = event.clipboardData;
  const html = clipboard?.getData("text/html") || "";
  const plainText = clipboard?.getData("text/plain") || "";
  if (!html.trim() && !plainText) {
    if (clipboardContainsFiles(clipboard)) {
      event.preventDefault();
      inlineError.value = "编辑器不支持直接粘贴图片或文件，请使用图片识别或 PDF 工具导入。";
    }
    return;
  }

  event.preventDefault();
  if (insertSanitizedClipboardContent(
    documentContentEditor.value,
    html,
    plainText,
  )) saveDocumentEditor();
  else if (!plainText) inlineError.value = "剪贴板里没有可粘贴的文字；图片请使用图片识别工具导入。";
}

watch(() => documentOutline.value.map(item => item.id).join("|"), () => {
  const ids = documentOutline.value.map(item => item.id);
  if (!ids.includes(activeOutlineId.value)) activeOutlineId.value = ids[0] || "";
  nextTick(updateActiveOutline);
});

function toggleDocumentPane() {
  if (documentPaneOpen.value) documentPersistence.flush();
  documentPaneOpen.value = !documentPaneOpen.value;
  const openBlankEditor = documentPaneOpen.value && !assistantState.documentContent.trim();
  if (openBlankEditor) setEditorMode("edit");
  if (documentPaneOpen.value && documentPaneElement.value?.clientWidth < 760) outlineCollapsed.value = true;
  if (window.matchMedia("(max-width: 760px)").matches) {
    mobilePane.value = documentPaneOpen.value ? "document" : "assistant";
  }
}

function openDocumentForEditing() {
  documentPaneOpen.value = true;
  if (window.matchMedia("(max-width: 760px)").matches) mobilePane.value = "document";
  setEditorMode("edit");
  nextTick(fitOutlineToDocumentPane);
}

function fitOutlineToDocumentPane() {
  if (!props.active) return;
  if (documentPaneElement.value?.clientWidth < 760) outlineCollapsed.value = true;
}

function migrateUntitledDocumentHeading() {
  if (assistantState.documentTitle.trim()) return;
  const documentDraft = splitDocumentTitle(assistantState.documentContent);
  if (!documentDraft.title) return;
  assistantState.documentTitle = documentDraft.title;
  assistantState.documentContent = documentDraft.content;
  persistDocument();
}

function updateFullscreenTarget() {
  const current = window.document.fullscreenElement;
  fullscreenTarget.value = current === documentPaneElement.value
    ? "document"
    : current === assistantPanelElement.value ? "assistant" : "";
}

async function togglePaneFullscreen(target) {
  const element = target === "document" ? documentPaneElement.value : assistantPanelElement.value;
  if (!element?.requestFullscreen) {
    inlineError.value = "当前浏览器不支持全屏显示";
    return;
  }
  try {
    if (window.document.fullscreenElement === element) await window.document.exitFullscreen();
    else {
      if (window.document.fullscreenElement) await window.document.exitFullscreen();
      await element.requestFullscreen();
    }
  } catch {
    inlineError.value = "无法进入全屏，请检查浏览器权限";
  }
}

function insertIntoDocument(content, replace = false) {
  const text = String(content || "").trim();
  if (!text) return;
  const existing = assistantState.documentContent.trim();
  assistantState.documentContent = replace || !existing ? text : `${existing}\n\n${text}`;
  persistDocument();
  inlineError.value = "已写入文档";
  if (window.matchMedia("(max-width: 760px)").matches) mobilePane.value = "document";
}

async function copyDocument() {
  const text = [assistantState.documentTitle.trim(), assistantState.documentContent.trim()].filter(Boolean).join("\n\n");
  if (!text) {
    inlineError.value = "文档还是空的";
    return;
  }
  try {
    await copyTextToClipboard(text);
    inlineError.value = "文档已复制";
  } catch {
    await openManualCopy(text);
  }
}

async function openManualCopy(text) {
  if (!manualCopyDialog.value) {
    inlineError.value = "自动复制失败，请重新选择文字后按 Ctrl+C 复制";
    return;
  }
  await manualCopyDialog.value.open(text);
  inlineError.value = "自动复制受限，文本已选中；请按 Ctrl+C（Mac 按 ⌘+C）复制";
}

function saveBlob(blob, filename) {
  downloadBlob(blob, filename, window.document);
}

async function downloadMarkdown() {
  const title = assistantState.documentTitle.trim() || "未命名文档";
  const content = assistantState.documentContent.trim();
  if (!content) {
    inlineError.value = "文档还是空的";
    return;
  }
  const markdown = `# ${title}\n\n${content}\n`;
  saveBlob(new Blob(["\ufeff", markdown], { type: "text/markdown;charset=utf-8" }), `${safeDownloadStem(title)}.md`);
  closeToolbarDisclosure("export", true);
}

async function downloadOfficeFormat(format) {
  const title = assistantState.documentTitle.trim() || "未命名文档";
  const content = assistantState.documentContent.trim();
  if (!content) {
    inlineError.value = "文档还是空的";
    return;
  }
  closeToolbarDisclosure("export", true);
  inlineError.value = "正在生成文件…";
  try {
    const response = await fetch(requestUrl(`/api/v1/assistant/export/${format}`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, content }),
    });
    if (!response.ok) {
      throw new Error(await responseErrorMessage(response, "文件生成失败，请稍后重试"));
    }
    const blob = await response.blob();
    const extension = format === "docx" ? "docx" : "pdf";
    saveBlob(blob, `${safeDownloadStem(title)}.${extension}`);
    inlineError.value = "文件已生成并开始下载";
  } catch (error) {
    inlineError.value = error?.message || "文件生成失败，请稍后重试";
  }
}

function resizeComposer() {
  if (!composer.value) return;
  composer.value.style.height = "auto";
  composer.value.style.height = `${Math.min(composer.value.scrollHeight, 200)}px`;
}

function updateMode() {
  if (activeConversation.value && !activeMessages.value.length) {
    activeConversation.value.mode = assistantState.mode;
    persistAssistantState();
  }
}

function scrollToLatest(behavior = "smooth", preserveScrollPosition = false) {
  if (!props.active) return;
  if (preserveScrollPosition && !followMessageScroll.value) return;
  nextTick(() => {
    if (!props.active) return;
    if (preserveScrollPosition && !followMessageScroll.value) return;
    const element = messageList.value;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior });
    followMessageScroll.value = true;
  });
}

function scheduleStreamPersistence(conversation) {
  streamPersistence.schedule(conversation);
}

function flushStreamPersistence() {
  streamPersistence.flush();
}

function scheduleMessageScrollToLatest(conversation) {
  if (!props.active || assistantState.activeId !== conversation.id || !followMessageScroll.value) return;
  if (streamMessageScrollFrame) return;
  streamMessageScrollFrame = window.requestAnimationFrame(() => {
    streamMessageScrollFrame = 0;
    if (!props.active || assistantState.activeId !== conversation.id || !followMessageScroll.value) return;
    const element = messageList.value;
    if (element) element.scrollTop = element.scrollHeight;
  });
}

function scheduleDocumentScrollToLatest(conversation) {
  if (!props.active || assistantState.activeId !== conversation.id || !followDocumentScroll.value || streamDocumentScrollFrame) return;
  streamDocumentScrollFrame = window.requestAnimationFrame(() => {
    streamDocumentScrollFrame = 0;
    if (!props.active || assistantState.activeId !== conversation.id || !followDocumentScroll.value) return;
    const element = documentPageScrollElement.value;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
    followDocumentScroll.value = true;
    updateActiveOutline();
  });
}

async function sendMessage() {
  const content = draft.value.trim();
  if (!content || sending.value) return;
  if (!assistantState.status.configured) {
    inlineError.value = "AI 模型还未接入。请在后端环境配置中设置模型 API，然后重启后端。";
    return;
  }

  const requestMode = assistantState.mode;
  const conversation = activeConversation.value || createConversation(requestMode);
  const requestWorkflow = resolveAssistantWorkflow(requestMode, content, conversation.messages);
  const isChatWorkflow = requestWorkflow === "chat";
  conversation.mode = requestMode;
  const userMessage = { id: createMessageId(), role: "user", content, createdAt: Date.now() };
  conversation.messages.push(userMessage);
  if (conversation.messages.filter(item => item.role === "user").length === 1) {
    conversation.title = content.replace(/\s+/g, " ").slice(0, 24) || "新对话";
  }
  conversation.updatedAt = Date.now();
  setAssistantConversationDraft(conversation.id, "");
  draft.value = "";
  inlineError.value = "";
  resizeComposer();
  persistAssistantState();
  currentRequestWorkflow.value = requestWorkflow;
  currentRequestConversationId.value = conversation.id;
  assistantState.requestConversationId = conversation.id;
  assistantState.requestActive = true;
  sending.value = true;
  window.dispatchEvent(new CustomEvent("iscan:assistant-stream-state", { detail: { active: true } }));
  streamingMessageId.value = "";
  followMessageScroll.value = true;
  if (!isChatWorkflow) followDocumentScroll.value = true;
  let assistantMessage = null;
  documentPaneOpen.value = !isChatWorkflow;
  if (isChatWorkflow) mobilePane.value = "assistant";
  if (!isChatWorkflow) editorMode.value = "read";
  scrollToLatest();
  let pendingStreamDelta = "";
  const renderDocumentDraft = () => {
    if (!assistantMessage) return;
    const documentDraft = splitDocumentTitle(assistantMessage.content);
    updateConversationDocument(conversation, documentDraft.title || "正在生成文档…", documentDraft.content, { persist: false, scroll: false });
    scheduleDocumentScrollToLatest(conversation);
  };
  const documentStreamRender = createStreamRenderScheduler(renderDocumentDraft, DOCUMENT_STREAM_RENDER_DELAY_MS);
  const streamRender = createStreamRenderScheduler(() => {
    if (!assistantMessage || !pendingStreamDelta) return;
    assistantMessage.content += pendingStreamDelta;
    pendingStreamDelta = "";
    scheduleStreamPersistence(conversation);
    if (isChatWorkflow) scheduleMessageScrollToLatest(conversation);
    else documentStreamRender.schedule();
  });

  try {
    const response = await fetch(requestUrl("/api/v1/assistant/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: requestWorkflow,
        stream: true,
        messages: buildAssistantRequestMessages(conversation),
      }),
    });
    if (!response.ok) {
      throw new Error(await responseErrorMessage(response, "请求失败，请稍后重试"));
    }
    if (!response.body) throw new Error("浏览器无法读取模型的流式响应");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    let streamFinished = false;
    const consumeLine = line => {
      if (!line.trim()) return;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        throw new Error("模型返回了无法识别的流式内容");
      }
      if (event.type === "delta" && typeof event.content === "string") {
        if (!assistantMessage) {
          assistantMessage = {
            id: createMessageId(),
            role: "assistant",
            content: "",
            createdAt: Date.now(),
            workflow: requestWorkflow,
            documentOutput: !isChatWorkflow,
            status: "streaming",
          };
          streamingMessageId.value = assistantMessage.id;
          conversation.messages.push(assistantMessage);
          if (!isChatWorkflow) scheduleMessageScrollToLatest(conversation);
        }
        pendingStreamDelta += event.content;
        streamRender.schedule();
      } else if (event.type === "error") {
        throw new Error(event.detail || "模型生成失败，请稍后重试");
      } else if (event.type === "done") {
        streamFinished = true;
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      let newlineIndex;
      while ((newlineIndex = pending.indexOf("\n")) !== -1) {
        consumeLine(pending.slice(0, newlineIndex).replace(/\r$/, ""));
        pending = pending.slice(newlineIndex + 1);
      }
      if (done) break;
    }
    if (pending.trim()) consumeLine(pending.replace(/\r$/, ""));
    streamRender.flush();
    if (!streamFinished) throw new Error("模型连接中断，请重试");
    if (!assistantMessage?.content) throw new Error("模型没有返回正文，请调整要求后重试");
    documentStreamRender.cancel();
    assistantMessage.status = "complete";
    conversation.updatedAt = Date.now();
    if (!isChatWorkflow) {
      flushStreamPersistence();
      const documentDraft = splitDocumentTitle(assistantMessage.content);
      updateConversationDocument(conversation, documentDraft.title || fallbackDocumentTitle(content), documentDraft.content, { preserveScrollPosition: true });
    } else {
      scheduleStreamPersistence(conversation);
      flushStreamPersistence();
    }
    if (assistantState.activeId === conversation.id) scrollToLatest("smooth", true);
  } catch (error) {
    streamRender.flush();
    documentStreamRender.flush();
    const errorMessage = error?.message || "请求失败，请稍后重试";
    if (assistantState.activeId === conversation.id) inlineError.value = errorMessage;
    if (assistantMessage) assistantMessage.status = "interrupted";
    conversation.updatedAt = Date.now();
    flushStreamPersistence();
    persistAssistantState();
    if (String(errorMessage).includes("尚未配置")) refreshStatus();
  } finally {
    documentStreamRender.cancel();
    const streamSaved = await flushAssistantPersistence();
    assistantState.requestActive = false;
    assistantState.requestConversationId = "";
    sending.value = false;
    currentRequestWorkflow.value = "";
    currentRequestConversationId.value = "";
    streamingMessageId.value = "";
    if (streamSaved) {
      window.dispatchEvent(new CustomEvent("iscan:assistant-stream-state", { detail: { active: false } }));
    }
  }
}

function onComposerKeydown(event) {
  handleAssistantComposerKeydown(event, sendMessage);
}

function createMessageId() {
  return createId();
}

async function copyMessage(content) {
  try {
    await copyTextToClipboard(content);
    inlineError.value = "已复制到剪贴板";
    window.setTimeout(() => { if (inlineError.value === "已复制到剪贴板") inlineError.value = ""; }, 1800);
  } catch {
    await openManualCopy(content);
  }
}

watch(() => props.apiBase, refreshStatus, { immediate: true });
watch(() => props.active, active => {
  if (!active) {
    inactiveMessageScrollTop = messageList.value?.scrollTop || 0;
    inactiveDocumentScrollTop = documentPageScrollElement.value?.scrollTop || 0;
    activeOutlineFrame.cancel();
    if (streamMessageScrollFrame) {
      window.cancelAnimationFrame(streamMessageScrollFrame);
      streamMessageScrollFrame = 0;
    }
    if (streamDocumentScrollFrame) {
      window.cancelAnimationFrame(streamDocumentScrollFrame);
      streamDocumentScrollFrame = 0;
    }
    flushPendingPersistence();
    return;
  }

  if (editorMode.value === "edit") refreshDocumentEditor();
  nextTick(() => {
    fitOutlineToDocumentPane();
    restoreScrollPosition(messageList.value, inactiveMessageScrollTop, followMessageScroll.value);
    restoreScrollPosition(documentPageScrollElement.value, inactiveDocumentScrollTop, followDocumentScroll.value);
    updateActiveOutline();
  });
});
watch(draft, value => {
  if (!assistantState.ready || !draftConversationId) return;
  const conversation = setAssistantConversationDraft(draftConversationId, value);
  if (conversation) streamPersistence.schedule(conversation);
});
watch(() => assistantState.ready, ready => {
  if (!ready) return;
  draftConversationId = assistantState.activeId || "";
  draft.value = getAssistantConversationDraft(draftConversationId);
  nextTick(resizeComposer);
  migrateLegacyAutoChat(activeConversation.value);
  cleanLoadedDocument();
  migrateUntitledDocumentHeading();
  documentPaneOpen.value = shouldOpenDocumentPane(assistantState.mode, lastAssistantMessage.value?.workflow);
  if (!documentPaneOpen.value && typeof window !== "undefined" && window.matchMedia("(max-width: 760px)").matches) mobilePane.value = "assistant";
}, { immediate: true });
watch(() => assistantState.activeId, (id, previousId) => {
  visibleMessageCount.value = INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT;
  documentPersistence.cancel();
  if (assistantState.ready && draftConversationId !== id) {
    const previousDraft = setAssistantConversationDraft(draftConversationId, draft.value);
    if (previousDraft) {
      streamPersistence.schedule(previousDraft);
      persistAssistantState();
    }
    const nextDraft = resolveAssistantDraftOnConversationChange({
      previousConversationId: previousId,
      draftConversationId,
      currentDraft: draft.value,
      nextDraft: getAssistantConversationDraft(id),
    });
    draftConversationId = id || "";
    if (nextDraft.preserveUnassignedDraft && id && nextDraft.value) {
      setAssistantConversationDraft(id, nextDraft.value);
      persistAssistantState();
    }
    draft.value = nextDraft.value;
  }
  followMessageScroll.value = true;
  followDocumentScroll.value = true;
  inlineError.value = "";
  if (assistantState.ready) {
    migrateLegacyAutoChat(activeConversation.value);
    cleanLoadedDocument();
  }
  documentPaneOpen.value = shouldOpenDocumentPane(assistantState.mode, lastAssistantMessage.value?.workflow);
  if (!documentPaneOpen.value && window.matchMedia("(max-width: 760px)").matches) mobilePane.value = "assistant";
  nextTick(() => {
    resizeComposer();
    if (assistantState.ready) migrateUntitledDocumentHeading();
    if (editorMode.value === "edit") refreshDocumentEditor();
    documentPageScrollElement.value?.scrollTo({ top: 0 });
    activeOutlineId.value = documentOutline.value[0]?.id || "";
    fitOutlineToDocumentPane();
  });
  scrollToLatest();
});
watch(() => assistantState.mode, mode => {
  updateMode();
  documentPaneOpen.value = shouldOpenDocumentPane(mode, lastAssistantMessage.value?.workflow);
  if (!documentPaneOpen.value && window.matchMedia("(max-width: 760px)").matches) mobilePane.value = "assistant";
});
watch(() => activeMessages.value.length, count => {
  if (count) nextTick(fitOutlineToDocumentPane);
});
onMounted(() => {
  resizeComposer();
  scrollToLatest("auto");
  nextTick(() => {
    fitOutlineToDocumentPane();
    updateActiveOutline();
  });
  window.addEventListener("resize", fitOutlineToDocumentPane);
  window.document.addEventListener("fullscreenchange", updateFullscreenTarget);
  window.addEventListener("pagehide", flushPendingPersistence);
});
onBeforeUnmount(() => {
  flushPendingPersistence();
  documentPersistence.cancel();
  activeOutlineFrame.cancel();
  if (streamMessageScrollFrame) window.cancelAnimationFrame(streamMessageScrollFrame);
  if (streamDocumentScrollFrame) window.cancelAnimationFrame(streamDocumentScrollFrame);
  window.removeEventListener("resize", fitOutlineToDocumentPane);
  window.document.removeEventListener("fullscreenchange", updateFullscreenTarget);
  window.removeEventListener("pagehide", flushPendingPersistence);
});
</script>

<template>
  <div v-if="!activeMessages.length" class="assistant-home">
    <header class="assistant-home-top">
      <div class="assistant-home-brand"><img :src="logoUrl" alt="" /><span>DocuCraft AI 工作台</span></div>
    </header>
    <main class="assistant-home-main">
      <div class="assistant-home-intro">
        <h1>你好，今天想完成什么？</h1>
        <p>写新内容、修改原文，或先说说你的想法。</p>
      </div>
      <div class="assistant-home-composer-wrap">
        <div v-if="requestIsInAnotherConversation" class="assistant-other-request">
          <span role="status" aria-live="polite">“{{ requestConversation?.title || '另一条对话' }}”正在生成。当前草稿会保留，完成后即可发送。</span>
          <button type="button" @click="openRequestConversation">查看进度</button>
        </div>
        <div v-if="assistantState.localSaveFailed" class="assistant-inline-error" role="alert">{{ SAVE_FAILURE_TEXT }}</div>
        <div v-if="inlineError" class="assistant-inline-error" :class="{ 'is-success': ['已写入文档', '文档已复制', '已复制到剪贴板', '文件已生成并开始下载'].includes(inlineError) }" role="status">{{ inlineError }}</div>
        <div v-if="!assistantState.status.loading && !assistantState.status.configured" class="assistant-setup-note" role="status">
          <div class="assistant-setup-copy"><strong>AI 助手暂不可用</strong><span>模型服务由服务器端连接，密钥不会下发到浏览器。</span></div>
          <button type="button" @click="refreshStatus">重新检测</button>
        </div>
        <div class="assistant-composer assistant-home-composer">
          <textarea ref="composer" v-model="draft" rows="2" maxlength="20000" aria-label="向 DocuCraft AI 助手提问" placeholder="输入问题，或粘贴你希望修改的原文…" @input="resizeComposer" @keydown="onComposerKeydown"></textarea>
          <div class="assistant-composer-footer">
            <div class="assistant-composer-options">
              <label class="assistant-mode-select"><span>工作流</span><select v-model="assistantState.mode" :disabled="sending" aria-label="选择写作工作流"><option v-for="option in MODE_OPTIONS" :key="option.id" :value="option.id">{{ option.label }}</option></select></label>
              <span class="assistant-mode-hint">{{ selectedMode.hint }}</span>
            </div>
            <div class="assistant-send-area"><small>Enter 发送 · Shift + Enter 换行</small><button class="assistant-send-button" type="button" :disabled="!draft.trim() || sending || assistantState.status.loading" :aria-label="sending ? (requestBelongsToActiveConversation ? '生成中' : '等待另一条对话完成') : '开始生成'" @click="sendMessage">{{ sending ? (requestBelongsToActiveConversation ? "生成中…" : "等待中…") : "开始生成 ↑" }}</button></div>
          </div>
        </div>
        <div class="assistant-home-starters">
          <button v-for="starter in starters" :key="starter.mode" type="button" class="assistant-home-starter" @click="chooseStarter(starter)">
            <span class="assistant-starter-icon">{{ starter.icon }}</span><strong>{{ starter.title }}</strong><small>{{ starter.description }}</small>
          </button>
        </div>
      </div>
      <p class="assistant-home-footnote">图片识别、历史文档和 PDF 编辑器仍可从左侧栏进入</p>
    </main>
  </div>

  <div v-else class="assistant-workspace" :class="{ 'is-swapped': panelsSwapped, 'is-chat': isChatLayout, 'is-document-closed': !documentPaneOpen }">
    <div v-if="documentPaneOpen" class="assistant-mobile-tabs" role="group" aria-label="工作区视图">
      <button type="button" :aria-pressed="mobilePane === 'document'" :class="{ 'is-active': mobilePane === 'document' }" @click="mobilePane = 'document'">文档</button>
      <button type="button" :aria-pressed="mobilePane === 'assistant'" :class="{ 'is-active': mobilePane === 'assistant' }" @click="mobilePane = 'assistant'">AI 助手</button>
    </div>

    <section ref="documentPaneElement" class="document-pane" :class="{ 'is-mobile-hidden': !documentPaneOpen || mobilePane !== 'document' }" aria-label="文档编辑区">
      <header class="document-toolbar">
        <div class="document-file-label">
          <span class="document-file-icon" aria-hidden="true"><svg class="toolbar-icon" viewBox="0 0 24 24" focusable="false"><path d="M6 3h8l4 4v14H6zM14 3v5h5M9 12h6M9 16h6" /></svg></span>
          <div><strong>{{ assistantState.documentTitle || "未命名文档" }}</strong><small v-if="assistantState.localSaveFailed" class="is-save-error" title="本地保存失败，请复制或下载备份" role="status">保存失败</small></div>
        </div>
        <div class="document-actions">
          <button class="document-tool-button pane-fullscreen-button" type="button" :aria-label="fullscreenTarget === 'document' ? '退出文档全屏' : '文档全屏'" :title="fullscreenTarget === 'document' ? '退出全屏（Esc）' : '文档全屏'" @click="togglePaneFullscreen('document')">
            <svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path v-if="fullscreenTarget === 'document'" d="M8 3v5H3m13-5v5h5M3 16h5v5m8 0v-5h5" />
              <path v-else d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m8 0h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
          <div class="document-menu-wrap" @keydown.esc.stop.prevent="closeToolbarDisclosure('editor', true)">
            <button ref="editorMenuTrigger" class="document-tool-button document-menu-trigger" type="button" aria-controls="document-mode-options" :aria-expanded="editorMenuOpen" @click="toggleToolbarDisclosure('editor')">
              <span>{{ editorMode === "edit" ? "编辑" : "阅读" }}</span>
              <svg class="toolbar-icon toolbar-chevron-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6" /></svg>
            </button>
            <div v-show="editorMenuOpen" id="document-mode-options" class="document-menu" role="group" aria-label="文档显示模式">
              <button type="button" :class="{ 'is-selected': editorMode === 'edit' }" @click="setEditorMode('edit')"><svg class="menu-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m14 5 5 5M4 20l4.5-1 10.8-10.8a2.1 2.1 0 0 0-3-3L5.5 16z" /></svg><span><strong>编辑</strong><small>可编辑文档</small></span><svg v-if="editorMode === 'edit'" class="menu-selected-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m5 12 4 4L19 6" /></svg></button>
              <button type="button" :class="{ 'is-selected': editorMode === 'read' }" @click="setEditorMode('read')"><svg class="menu-action-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M2.5 12s3.4-6 9.5-6 9.5 6 9.5 6-3.4 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.5" /></svg><span><strong>阅读</strong><small>专注阅读与校对</small></span><svg v-if="editorMode === 'read'" class="menu-selected-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m5 12 4 4L19 6" /></svg></button>
            </div>
          </div>
          <button class="document-tool-button" type="button" @click="copyDocument">复制</button>
          <div class="document-menu-wrap" @keydown.esc.stop.prevent="closeToolbarDisclosure('export', true)">
            <button ref="exportMenuTrigger" class="document-tool-button document-menu-trigger" type="button" aria-controls="document-export-options" :aria-expanded="exportMenuOpen" @click="toggleToolbarDisclosure('export')">
              <svg class="toolbar-icon toolbar-download-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 3v12m-5-5 5 5 5-5M5 20h14" /></svg>
              <span>下载</span>
              <svg class="toolbar-icon toolbar-chevron-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6" /></svg>
            </button>
            <div v-show="exportMenuOpen" id="document-export-options" class="document-menu export-menu" role="group" aria-label="下载格式">
              <button type="button" @click="downloadOfficeFormat('docx')"><span class="export-icon word">W</span><strong>Word</strong></button>
              <button type="button" @click="downloadOfficeFormat('pdf')"><span class="export-icon pdf">P</span><strong>PDF</strong></button>
              <button type="button" @click="downloadMarkdown"><span class="export-icon markdown">M</span><strong>Markdown</strong></button>
            </div>
          </div>
          <button class="document-tool-button document-close-button" type="button" aria-label="关闭文档区" title="关闭文档区" @click="toggleDocumentPane">
            <svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
      </header>

      <div class="document-workspace-body">
        <nav v-if="documentOutline.length" class="document-outline" :class="{ 'is-collapsed': outlineCollapsed }" aria-label="文档目录">
          <button class="document-outline-toggle" type="button" :aria-expanded="!outlineCollapsed" :aria-label="outlineCollapsed ? '展开文档目录' : '收起文档目录'" :title="outlineCollapsed ? '展开目录' : '收起目录'" @click="outlineCollapsed = !outlineCollapsed"><svg class="outline-toggle-icon" :class="{ 'is-collapsed': outlineCollapsed }" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m14 5-7 7 7 7" /></svg></button>
          <div v-if="!outlineCollapsed" class="document-outline-content">
            <button class="document-outline-document-title" type="button" @click="scrollDocumentToStart">{{ assistantState.documentTitle || "未命名文档" }}</button>
            <div class="document-outline-items">
              <button v-for="item in documentOutline" :key="item.id" class="document-outline-item" :class="{ 'is-active': activeOutlineId === item.id }" :style="{ paddingInlineStart: `${8 + (item.level - 1) * 12}px` }" :aria-current="activeOutlineId === item.id ? 'location' : undefined" :title="item.title" type="button" @click="navigateToOutline(item)">{{ item.title }}</button>
            </div>
          </div>
        </nav>

        <main ref="documentPageScrollElement" class="document-page-scroll" @scroll.passive="onDocumentScroll">
          <article class="document-page">
            <input v-model="assistantState.documentTitle" class="document-title-input" aria-label="文档标题" placeholder="请输入标题" @input="scheduleDocumentPersistence" @blur="documentPersistence.flush()" />
            <div v-if="props.active && (assistantState.documentContent || editorMode === 'edit')" class="document-content-wrap">
              <div v-if="editorMode === 'edit'" ref="documentContentEditor" class="document-content-editor" contenteditable="true" role="textbox" aria-label="文档正文" aria-multiline="true" data-placeholder="在这里编辑文档…" spellcheck="true" v-html="documentEditorHtml" @input="saveDocumentEditor" @paste="pasteDocumentEditor" @blur="documentPersistence.flush()"></div>
              <div v-else class="document-content-reading" v-html="renderedDocument.html"></div>
            </div>
            <div v-else-if="sending && currentRequestWorkflow !== 'chat'" class="document-empty-placeholder">正在撰写文档…</div>
            <div v-else class="document-empty-placeholder" aria-label="空白文档">文档内容会显示在这里</div>
          </article>
        </main>
        <button v-if="!followDocumentScroll" class="scroll-to-latest document-scroll-to-latest" type="button" @click="scrollDocumentToLatest()">↓ 回到文档末尾</button>
      </div>
    </section>

    <aside ref="assistantPanelElement" class="assistant-panel" :class="{ 'is-mobile-hidden': mobilePane !== 'assistant' }" aria-label="AI 助手">
      <header class="assistant-panel-header">
        <div class="assistant-panel-brand"><img :src="logoUrl" alt="" /><div><strong>DocuCraft 助手</strong><small>{{ isChatLayout ? "轻松聊聊，也可随时打开文档" : "与你一起完成文档" }}</small></div></div>
        <div class="assistant-panel-tools">
          <button class="assistant-panel-tool" type="button" :aria-expanded="documentPaneOpen" :aria-label="documentPaneOpen ? '关闭文档区' : '打开文档'" :title="documentPaneOpen ? '关闭文档区' : '打开文档'" @click="toggleDocumentPane">
            <svg v-if="documentPaneOpen" class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 5h18v14H3zM14 5v14" /></svg>
            <svg v-else class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 3h8l4 4v14H6zM14 3v5h5M9 12h6M9 16h6" /></svg>
          </button>
          <button v-if="documentPaneOpen" class="assistant-panel-tool pane-swap-button" type="button" aria-label="交换文档与 AI 面板位置" title="交换左右位置" @click="panelsSwapped = !panelsSwapped"><svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 8h15m-4-4 4 4-4 4M20 16H5m4 4-4-4 4-4" /></svg></button>
          <button class="assistant-panel-tool" type="button" :aria-label="fullscreenTarget === 'assistant' ? '退出 AI 面板全屏' : 'AI 面板全屏'" :title="fullscreenTarget === 'assistant' ? '退出全屏（Esc）' : 'AI 面板全屏'" @click="togglePaneFullscreen('assistant')">
            <svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path v-if="fullscreenTarget === 'assistant'" d="M8 3v5H3m13-5v5h5M3 16h5v5m8 0v-5h5" />
              <path v-else d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m8 0h3a2 2 0 0 0 2-2v-3" />
            </svg>
          </button>
        </div>
      </header>

      <section ref="messageList" class="assistant-thread" aria-live="polite" aria-label="AI 对话" @scroll.passive="onMessageScroll">
        <button v-if="hasOlderMessages" class="assistant-load-older" type="button" aria-controls="assistant-messages" @click="loadOlderMessages">加载更早消息（还有 {{ activeMessages.length - visibleMessages.length }} 条）</button>
        <div id="assistant-messages" class="assistant-messages">
          <article v-for="message in visibleMessages" :key="message.id" class="assistant-message" :class="`is-${message.role}`">
            <div class="assistant-avatar" :class="`is-${message.role}`">{{ message.role === "assistant" ? "AI" : "我" }}</div>
            <div class="assistant-message-body">
              <div class="assistant-message-label">
                {{ message.role === "assistant" ? "DocuCraft 助手" : "你" }}
                <span v-if="message.status && (!message.workflow || message.workflow === 'chat')" class="assistant-message-status">{{ assistantMessageStatus(message) === "streaming" ? "正在回复…" : assistantMessageStatus(message) === "complete" ? "回复完成" : "回复中断，已保留内容" }}</span>
              </div>
              <div v-if="message.role === 'assistant' && message.workflow && message.workflow !== 'chat'" class="assistant-document-status-card" :class="`is-${documentTaskStatus(message).icon}`">
                <span class="assistant-document-status-icon" aria-hidden="true"><i v-if="message.status === 'streaming'"></i><template v-else>{{ message.status === 'complete' ? '✓' : '!' }}</template></span>
                <div class="assistant-document-status-copy"><strong>{{ documentTaskStatus(message).title }}</strong><p>{{ documentTaskStatus(message).detail }}</p></div>
                <button v-if="message.id === latestDocumentId && assistantMessageStatus(message) !== 'streaming'" class="assistant-document-open-button" type="button" @click="openDocumentForEditing">打开文档编辑</button>
              </div>
              <div v-else-if="props.active" class="assistant-message-content" v-html="renderAssistantMessage(message)"></div>
              <div v-if="message.role === 'assistant' && (!message.workflow || message.workflow === 'chat')" class="assistant-message-actions">
                <button v-if="!message.workflow && assistantState.mode !== 'chat'" type="button" @click="insertIntoDocument(message.content)">写入文档</button>
                <button v-if="!message.workflow && assistantState.mode !== 'chat'" type="button" @click="insertIntoDocument(message.content, true)">替换正文</button>
                <button type="button" @click="copyMessage(message.content)">复制</button>
              </div>
            </div>
          </article>
          <div v-if="requestBelongsToActiveConversation && !streamingMessageId" class="assistant-message is-assistant">
            <div class="assistant-avatar is-assistant">AI</div>
            <div class="assistant-message-body"><div class="assistant-message-label">DocuCraft 助手</div><div class="assistant-thinking"><i></i><i></i><i></i><span>{{ currentRequestWorkflow === "chat" ? "正在回复…" : "正在准备写入左侧文档…" }}</span></div></div>
          </div>
        </div>
      </section>

      <div class="assistant-compose-area">
        <button v-if="!followMessageScroll" class="scroll-to-latest assistant-scroll-to-latest" type="button" @click="scrollToLatest()">↓ 回到最新消息</button>
        <div v-if="requestIsInAnotherConversation" class="assistant-other-request">
          <span role="status" aria-live="polite">“{{ requestConversation?.title || '另一条对话' }}”正在生成。当前草稿会保留，完成后即可发送。</span>
          <button type="button" @click="openRequestConversation">查看进度</button>
        </div>
        <div v-if="inlineError" class="assistant-inline-error" :class="{ 'is-success': ['已写入文档', '文档已复制', '已复制到剪贴板', '文件已生成并开始下载'].includes(inlineError) }" role="status">{{ inlineError }}</div>
        <div v-if="assistantState.localSaveFailed && (!documentPaneOpen || mobilePane === 'assistant')" class="assistant-inline-error assistant-save-warning" role="alert">{{ SAVE_FAILURE_TEXT }}</div>
        <div v-if="!assistantState.status.loading && !assistantState.status.configured" class="assistant-setup-note" role="status">
          <div class="assistant-setup-copy"><strong>AI 助手暂不可用</strong><span>模型服务由服务器端连接，密钥不会下发到浏览器。</span></div>
          <button type="button" @click="refreshStatus">重新检测</button>
        </div>
        <div class="assistant-composer">
          <textarea ref="composer" v-model="draft" rows="2" maxlength="20000" aria-label="向 DocuCraft AI 助手提问" placeholder="发消息或创建任务…" @input="resizeComposer" @keydown="onComposerKeydown"></textarea>
          <div class="assistant-composer-footer">
            <div class="assistant-composer-options">
              <label class="assistant-mode-select"><span>工作流</span><select v-model="assistantState.mode" :disabled="sending" aria-label="选择写作工作流"><option v-for="option in MODE_OPTIONS" :key="option.id" :value="option.id">{{ option.label }}</option></select></label>
            </div>
            <div class="assistant-send-area">
              <small>Enter 发送 · Shift + Enter 换行</small>
              <button class="assistant-send-button" type="button" :disabled="!draft.trim() || sending || assistantState.status.loading" :aria-label="sending ? (requestBelongsToActiveConversation ? '生成中' : '等待另一条对话完成') : '发送消息'" @click="sendMessage">{{ sending ? (requestBelongsToActiveConversation ? "生成中…" : "等待中…") : "发送 ↑" }}</button>
            </div>
          </div>
        </div>
        <p class="assistant-privacy-note">{{ isChatLayout ? "闲聊和一般问答留在对话中，不会自动写入文档。" : "写作与修改遵循当前工作流，并结合本轮对话继续迭代。" }}</p>
      </div>
    </aside>
  </div>

  <ManualCopyDialog ref="manualCopyDialog" />
</template>
