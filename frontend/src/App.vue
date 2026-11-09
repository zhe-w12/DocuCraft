<script setup>
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import AssistantWorkspace from "./AssistantWorkspace.vue";
import DocumentWorkspace from "./DocumentWorkspace.vue";
import ManualCopyDialog from "./ManualCopyDialog.vue";
import FileIcon from "./FileIcon.vue";
import { HISTORY_PAGE_SIZE, nextHistoryPageLimit, sliceHistoryPage } from "./history-pagination.js";
import { orderDocumentHistory } from "./history-order.js";
import { prepareImageEditorWorkspaceHtml } from "./editor-workspace-html.js";
import { formatImageImportFailure, friendlyError } from "./error-messages.js";
import { isBackendVersionCompatible } from "./backend-version.js";
import { responseError } from "./http-errors.js";
import { retryAfterRequest } from "./retry-after.js";
import { isSafeForAutomaticAppUpdate } from "./app-update-policy.js";
import { assistantState, createAssistantConversation, flushAssistantPersistence, isConversationGenerating, removeAssistantConversation, selectAssistantConversation } from "./assistant-state.js";
import { createServiceWorkerUpdateGate } from "./service-worker-update.js";
import { consumeAppUpdateView, persistAppUpdateView } from "./app-update-view.js";
import { isEditorFrameMessage, requestEditorDocument } from "./editor-frame-bridge.js";
import { ASSISTANT_DIALOG_HISTORY_PAGE_SIZE, ASSISTANT_SIDEBAR_HISTORY_LIMIT, nextAssistantConversationHistoryLimit, orderAssistantConversationHistory, sliceAssistantConversationHistory } from "./assistant-conversation-history.js";
import { LocalDocuments } from "./local-documents.js";
import { createId } from "./ids.js";
import { createHistoryTitleMatcher } from "./history-search.js";
import { createHistoryDateFormatter } from "./history-date.js";
import { commitHistoryBeforeFileMutation } from "./history-storage-transaction.js";
import { shareLinkWithFallback } from "./share-link.js";
import { canUploadImageUnchanged, isImageFile, prepareImage } from "./image-preparation.js";
import { resolveImageDocumentTitle } from "./image-document-title.js";
import { copyTextToClipboard } from "./clipboard.js";
import { focusElement } from "./focus-element.js";
import { safeDownloadStem } from "./download-filename.js";
import { downloadBlob } from "./browser-download.js";
import "../styles.css";

const PdfEditor = defineAsyncComponent(() => import("./PdfEditor.vue"));
const serviceWorkerUpdateGate = createServiceWorkerUpdateGate(() => window.location.reload());
const assistantStreamBusy = ref(false);
const remoteBuildId = ref("");
const dismissedBuildId = ref("");
const updateReloadRequested = ref(false);
const updatePreparing = ref(false);
const filePickerOpen = ref(false);
const hasAppUpdate = computed(() => Boolean(
  remoteBuildId.value
    && remoteBuildId.value !== __ISCAN_BUILD_ID__
    && remoteBuildId.value !== dismissedBuildId.value,
));
const onAssistantStreamState = event => {
  assistantStreamBusy.value = Boolean(event.detail?.active);
  serviceWorkerUpdateGate.setAssistantBusy(assistantStreamBusy.value);
};
const onServiceWorkerControllerChange = () => {
  // A newly activated worker can take control while an image/PDF import is
  // still running. Detect the new build, then let the normal update flow wait
  // for a safe point instead of reloading this page immediately.
  void checkForAppUpdate();
};
let serviceWorkerControllerChangeListening = false;
let appUpdateTimer = 0;
let appUpdateCheckInFlight = false;

const HISTORY_KEY = "scan-craft-web-history-v1";
const API_KEY = "scan-craft-web-api";
const FALLBACK_UPLOAD_LIMIT_BYTES = 18 * 1024 * 1024;
const HOST = window.location.hostname;
const ENV_API = String(import.meta.env.VITE_API_BASE_URL || "").trim().replace(/\/+$/, "");
const DEFAULT_API = ENV_API || `${window.location.protocol}//${HOST}:5000`;
const logoUrl = `${import.meta.env.BASE_URL}iscan-logo.png`;

function readSetting(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function normalizeApi(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function historyApi(item) {
  if (item.api) return normalizeApi(item.api);
  for (const value of [item.sourceImageUrl, item.thumb]) {
    try {
      const url = new URL(value);
      const marker = url.pathname.indexOf("/api/v1/documents/");
      if (marker >= 0) return `${url.origin}${url.pathname.slice(0, marker)}`;
    } catch { /* Older history entries may not have a source URL. */ }
  }
  return readSetting(API_KEY) || DEFAULT_API;
}

function readHistory() {
  try {
    const value = JSON.parse(readSetting(HISTORY_KEY) || "[]");
    if (!Array.isArray(value)) return [];
    return value
      .filter(item => item && typeof item.id === "string" && ["image", "pdf"].includes(item.type))
      .map(item => item.type === "image" ? { ...item, api: historyApi(item) } : item);
  } catch {
    return [];
  }
}

function readAppUpdateView() {
  try { return consumeAppUpdateView(window.sessionStorage); } catch { return null; }
}

function persistCurrentViewForUpdate() {
  try {
    persistAppUpdateView(window.sessionStorage, {
      view: state.view,
      activeTool: state.activeTool,
    });
  } catch { /* Session storage can be disabled by browser privacy settings. */ }
}

const restoredAppUpdateView = readAppUpdateView();

const state = reactive({
  api: ENV_API || readSetting(API_KEY) || DEFAULT_API,
  maxUploadBytes: FALLBACK_UPLOAD_LIMIT_BYTES,
  maxSourceImageBytes: FALLBACK_UPLOAD_LIMIT_BYTES,
  history: readHistory(),
  current: null,
  activeTool: restoredAppUpdateView?.activeTool || "image",
  view: restoredAppUpdateView?.view || "assistant",
  pdfFile: null,
  pdfId: null,
  pdfWorkspaceOpen: false,
  sessionPdfs: new Map(),
  editorDocument: null,
  editorBaseline: null,
  editorDirty: false,
  editorSrcdoc: "",
  busy: false,
  busyTitle: "正在处理",
  busyText: "请稍候…",
  saving: false,
  historySearch: "",
  historyFilter: "all",
  connectionStatus: "checking",
  connectionMessage: "正在检查服务",
  importStatus: "",
  failedImageCount: 0,
  pdfNotice: "",
  toast: "",
  toastVisible: false,
});

const canAutoApplyImageToolUpdate = computed(() => isSafeForAutomaticAppUpdate({
  hasAppUpdate: hasAppUpdate.value,
  assistantReady: assistantState.ready,
  assistantSaveFailed: assistantState.localSaveFailed,
  view: state.view,
  activeTool: state.activeTool,
  filePickerOpen: filePickerOpen.value,
  updatePreparing: updatePreparing.value,
  busy: state.busy,
  failedImageCount: state.failedImageCount,
  saving: state.saving,
  editorOpen: Boolean(state.editorDocument),
  pdfWorkspaceOpen: state.pdfWorkspaceOpen,
  reloadRequested: updateReloadRequested.value,
}));

watch(canAutoApplyImageToolUpdate, shouldApply => {
  if (shouldApply) void applyAppUpdate();
}, { flush: "post" });

const dragging = reactive({ image: false, pdf: false });
const apiDraft = ref("");
const apiError = ref("");
const settingsDialog = ref(null);
const conversationDialog = ref(null);
const conversationDialogOpen = ref(false);
const shareCopyDialog = ref(null);
const plainTextCopyDialog = ref(null);
const conversationHistoryVisibleCount = ref(ASSISTANT_DIALOG_HISTORY_PAGE_SIZE);
const apiInput = ref(null);
const editorFrame = ref(null);
const closeEditorButton = ref(null);
const pdfEditor = ref(null);
const pdfEditorModal = ref(null);
const imageImportButton = ref(null);
const imageInput = ref(null);
const pdfImportButton = ref(null);
const pdfToolNavButton = ref(null);
const pdfInputSecondary = ref(null);
const historyVisibleCount = ref(HISTORY_PAGE_SIZE);
const matchesHistoryTitle = createHistoryTitleMatcher();
const formatDate = createHistoryDateFormatter();
const orderedHistory = computed(() => orderDocumentHistory(state.history));
const recentImageHistory = computed(() => orderedHistory.value.filter(item => item.type === "image").slice(0, 3));
const recentPdfHistory = computed(() => orderedHistory.value.filter(item => item.type === "pdf").slice(0, 3));
let failedImageFiles = [];
let uploadLimitCheckedFor = "";
let toastTimer = 0;
let appUpdateInProgress = false;
let appUpdateAttemptSequence = 0;
let activeAppUpdateAttempt = 0;
let editorReturnFocus = null;
let pdfEditorReturnFocus = null;
let beforeUnloadHandler = null;

const assistantConversationHistory = computed(() => orderAssistantConversationHistory(assistantState.conversations));
const recentAssistantConversations = computed(() => sliceAssistantConversationHistory(
  assistantConversationHistory.value,
  ASSISTANT_SIDEBAR_HISTORY_LIMIT,
));
const visibleAssistantConversations = computed(() => sliceAssistantConversationHistory(
  assistantConversationHistory.value,
  conversationHistoryVisibleCount.value,
));
const hasMoreAssistantConversations = computed(() => (
  visibleAssistantConversations.value.length < assistantConversationHistory.value.length
));

watch(() => state.pdfWorkspaceOpen && !state.busy, async isInteractive => {
  if (!isInteractive) return;
  await nextTick();
  if (!state.pdfWorkspaceOpen || state.busy) return;
  focusElement(pdfEditorModal.value);
});

const matchingHistory = computed(() => {
  const query = state.historySearch.trim().toLocaleLowerCase();
  return orderedHistory.value.filter(item => {
    if (state.historyFilter !== "all" && item.type !== state.historyFilter) return false;
    return matchesHistoryTitle(item, query);
  });
});
const visibleHistory = computed(() => sliceHistoryPage(matchingHistory.value, historyVisibleCount.value));
const hasMoreHistory = computed(() => visibleHistory.value.length < matchingHistory.value.length);
watch(() => [state.historySearch, state.historyFilter], () => {
  historyVisibleCount.value = HISTORY_PAGE_SIZE;
});
const historyEmptyTitle = computed(() => {
  const filtered = Boolean(state.historySearch.trim()) || state.historyFilter !== "all";
  return filtered ? "没有找到匹配的文档" : "还没有历史文档";
});
const historyEmptyText = computed(() => {
  const filtered = Boolean(state.historySearch.trim()) || state.historyFilter !== "all";
  return filtered ? "试试其他关键词，或切换文档类型。" : "从上面的导入按钮开始创建第一份文档。";
});
function fileTitle(fileName) {
  return String(fileName || "扫描文档").replace(/\.[^.]+$/, "") || "扫描文档";
}

function historyIcon(item) {
  return item.type === "pdf" ? "pdf" : item.hasTables ? "table" : "text";
}

function historySubtitle(item) {
  const type = item.type === "pdf"
    ? (item.sessionOnly ? "PDF · 仅本次会话" : item.storage === "indexeddb" ? "PDF · 本地保存" : "PDF · 需重新导入")
    : item.hasTables ? "图片 · 表格文档" : "图片 · 文字文档";
  return `${type} · ${formatDate(item.createdAt)}`;
}

function loadMoreHistory() {
  historyVisibleCount.value = nextHistoryPageLimit(
    historyVisibleCount.value,
    matchingHistory.value.length,
  );
}

function apiUrl(path, api = state.api) {
  return `${normalizeApi(api)}${path}`;
}

function sendWithUploadProgress(url, options, onUploadProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(options.method || "GET", url, true);
    request.timeout = 150000;
    for (const [name, value] of Object.entries(options.headers || {})) {
      request.setRequestHeader(name, value);
    }
    request.upload.addEventListener("progress", event => {
      onUploadProgress({
        loaded: event.loaded,
        total: event.total,
        lengthComputable: event.lengthComputable,
      });
    });
    request.addEventListener("load", () => {
      const body = request.responseText || "";
      resolve({
        ok: request.status >= 200 && request.status < 300,
        status: request.status,
        headers: { get: name => request.getResponseHeader(name) },
        text: async () => body,
        json: async () => JSON.parse(body),
      });
    });
    request.addEventListener("error", () => reject(new Error("网络连接失败，请检查网络或服务状态")));
    request.addEventListener("timeout", () => reject(new Error("图片上传或识别超时，请稍后重试")));
    request.addEventListener("abort", () => reject(new Error("图片上传已取消")));
    request.send(options.body ?? null);
  });
}

async function requestDocument(path, options = {}, api = state.api) {
  const { onUploadProgress, ...requestOptions } = options;
  let response;
  let timer = null;
  try {
    if (typeof onUploadProgress === "function" && typeof XMLHttpRequest === "function") {
      response = await sendWithUploadProgress(apiUrl(path, api), requestOptions, onUploadProgress);
    } else {
      const controller = new AbortController();
      timer = window.setTimeout(() => controller.abort(), 150000);
      response = await fetch(apiUrl(path, api), { ...requestOptions, signal: controller.signal });
    }
    if (!response.ok) throw await responseError(response, `请求失败（${response.status}）`);
    const result = await response.json();
    if (!result || typeof result.id !== "string") throw new Error("服务返回的文档不完整，请重试");
    for (const key of ["docx_url", "structured_docx_url", "pdf_url", "structured_pdf_url", "source_image_url"]) {
      if (result[key]) result[key] = new URL(result[key], `${normalizeApi(api)}/`).href;
    }
    return { ...result, api: normalizeApi(api) };
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }
}

function writeHistory() {
  try {
    const history = state.history.filter(item => !item.sessionOnly).map(item => {
      if (item.type !== "pdf") return item;
      const { objectUrl, url, ...metadata } = item;
      return metadata;
    });
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history));
    return true;
  } catch { return false; }
}

function showToast(message) {
  state.toast = message;
  state.toastVisible = true;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { state.toastVisible = false; }, 2600);
}

async function checkForAppUpdate() {
  if (!import.meta.env.PROD || appUpdateCheckInFlight) return;
  appUpdateCheckInFlight = true;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 5000);
  try {
    const versionUrl = new URL(`${import.meta.env.BASE_URL}build-version.json`, window.location.href);
    versionUrl.searchParams.set("check", Date.now());
    const response = await fetch(versionUrl, { cache: "no-store", signal: controller.signal });
    if (!response.ok) return;
    const version = await response.json();
    if (typeof version.buildId === "string" && version.buildId !== __ISCAN_BUILD_ID__) {
      remoteBuildId.value = version.buildId;
    }
  } catch {
    /* An unavailable version marker should not interrupt the app. */
  } finally {
    window.clearTimeout(timer);
    appUpdateCheckInFlight = false;
  }
}

function checkForAppUpdateWhenVisible() {
  if (window.document.visibilityState === "visible") void checkForAppUpdate();
}

function checkForAppUpdateWhenOnline() {
  void checkApi();
  void checkForAppUpdate();
}

async function applyAppUpdate() {
  if (appUpdateInProgress || updatePreparing.value || updateReloadRequested.value) return;
  if (filePickerOpen.value) {
    showToast("请先完成文件选择，再刷新页面。");
    return;
  }
  appUpdateInProgress = true;
  const attempt = ++appUpdateAttemptSequence;
  activeAppUpdateAttempt = attempt;
  const isCurrentAttempt = () => activeAppUpdateAttempt === attempt;
  updatePreparing.value = true;
  try {
    if (!assistantState.ready) {
      showToast("请等本地对话记录加载完成后再刷新。");
      return;
    }
    if (state.busy || state.saving) {
      showToast("请先完成当前处理，并保存未完成的文档修改，再刷新。");
      return;
    }
    const editorChangedBeforeUpdate = await editorHasChanges();
    if (!isCurrentAttempt()) return;
    if (state.busy || state.saving || editorChangedBeforeUpdate) {
      showToast("请先完成当前处理，并保存未完成的文档修改，再刷新。");
      return;
    }
    let assistantSaved = false;
    try {
      assistantSaved = await flushAssistantPersistence();
    } catch {
      assistantSaved = false;
    }
    if (!isCurrentAttempt()) return;
    if (!assistantSaved) {
      showToast("AI 文档本地保存失败，请先复制重要内容；保存恢复后再刷新。");
      return;
    }

    // Persistence can wait on IndexedDB. Recheck the page after that wait so
    // an import or editor opened in the meantime is never discarded by reload.
    const editorChangedDuringUpdate = await editorHasChanges();
    if (!isCurrentAttempt()) return;
    if (
      state.busy
      || state.saving
      || editorChangedDuringUpdate
      || state.editorDocument
      || state.pdfWorkspaceOpen
      || filePickerOpen.value
    ) {
      showToast("检测到新的处理或编辑操作，已暂缓刷新；完成后再更新。");
      return;
    }
    if (pdfEditor.value?.hasEdits && !window.confirm("刷新会丢失尚未导出的 PDF 擦除和签名，确定继续吗？")) return;
    persistCurrentViewForUpdate();
    updateReloadRequested.value = true;
    serviceWorkerUpdateGate.requestReload();
  } finally {
    if (isCurrentAttempt()) {
      activeAppUpdateAttempt = 0;
      appUpdateInProgress = false;
      updatePreparing.value = false;
    }
  }
}

function dismissAppUpdate() {
  dismissedBuildId.value = remoteBuildId.value;
  if (updatePreparing.value && !updateReloadRequested.value) {
    activeAppUpdateAttempt = 0;
    appUpdateInProgress = false;
    updatePreparing.value = false;
    showToast("更新已暂缓，可以继续当前操作。");
  }
}

function setBusy(isBusy, title = "正在处理", text = "请稍候…") {
  state.busy = isBusy;
  if (isBusy) {
    state.busyTitle = title;
    state.busyText = text;
  }
}

async function checkApi(api = state.api) {
  if (state.api === api) {
    state.connectionStatus = "checking";
    state.connectionMessage = "正在检查服务";
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 4500);
  try {
    const response = await fetch(apiUrl("/health", api), { signal: controller.signal });
    if (!response.ok) throw new Error("服务不可用");
    const health = await response.json().catch(() => null);
    const reportedUploadLimit = Number(health?.max_upload_bytes);
    const hasReportedUploadLimit = Number.isSafeInteger(reportedUploadLimit) && reportedUploadLimit > 0;
    const maxUploadBytes = hasReportedUploadLimit
      ? reportedUploadLimit
      : FALLBACK_UPLOAD_LIMIT_BYTES;
    const reportedSourceLimit = Number(health?.max_source_image_bytes);
    const maxSourceImageBytes = Number.isSafeInteger(reportedSourceLimit) && reportedSourceLimit > 0
      ? Math.max(maxUploadBytes, reportedSourceLimit)
      : maxUploadBytes;
    if (state.api === api) {
      state.maxUploadBytes = maxUploadBytes;
      state.maxSourceImageBytes = maxSourceImageBytes;
      uploadLimitCheckedFor = api;
      const backendVersionCompatible = isBackendVersionCompatible(
        health,
        __ISCAN_API_COMPATIBILITY_VERSION__,
        __ISCAN_BACKEND_FINGERPRINT__,
      );
      state.connectionStatus = backendVersionCompatible ? "online" : "outdated";
      state.connectionMessage = backendVersionCompatible ? "后端已连接" : "后端版本需更新";
    }
    return maxUploadBytes;
  } catch {
    if (state.api === api) {
      state.maxUploadBytes = FALLBACK_UPLOAD_LIMIT_BYTES;
      state.maxSourceImageBytes = FALLBACK_UPLOAD_LIMIT_BYTES;
      state.connectionStatus = "offline";
      state.connectionMessage = "服务未连接";
    }
    return FALLBACK_UPLOAD_LIMIT_BYTES;
  } finally {
    window.clearTimeout(timer);
  }
}

function switchView(name) {
  state.view = name;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function switchTool(name) {
  if (name !== "image" && name !== "pdf") return;
  state.activeTool = name;
  state.view = "tools";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openAllHistory() {
  state.historyFilter = "all";
  state.historySearch = "";
  switchView("history");
}

function openToolHistory(type) {
  if (type !== "image" && type !== "pdf") return;
  state.historyFilter = type;
  state.historySearch = "";
  switchView("history");
}

function retryAssistantStorage() {
  window.location.reload();
}

function startAssistantConversation() {
  if (!assistantState.ready || assistantState.localLoadFailed) return;
  createAssistantConversation("auto");
  switchView("assistant");
}

function openAssistantConversation(id) {
  if (!assistantState.ready || assistantState.localLoadFailed) return;
  if (selectAssistantConversation(id)) {
    switchView("assistant");
  }
}

function deleteAssistantConversation(id) {
  if (!assistantState.ready || assistantState.localLoadFailed) return;
  if (isConversationGenerating(id)) return;
  removeAssistantConversation(id);
}

function openConversationHistory() {
  if (!assistantState.ready || assistantState.localLoadFailed) return;
  conversationHistoryVisibleCount.value = ASSISTANT_DIALOG_HISTORY_PAGE_SIZE;
  conversationDialogOpen.value = true;
  conversationDialog.value?.showModal();
}

function closeConversationHistory() {
  conversationDialogOpen.value = false;
  if (conversationDialog.value?.open) conversationDialog.value.close();
}

function loadMoreAssistantConversations() {
  conversationHistoryVisibleCount.value = nextAssistantConversationHistoryLimit(
    conversationHistoryVisibleCount.value,
    assistantConversationHistory.value.length,
  );
}

function openConversationFromHistory(id) {
  openAssistantConversation(id);
  closeConversationHistory();
}

function addHistory(item) {
  state.history = [item, ...state.history.filter(entry => entry.id !== item.id || (entry.api || "") !== (item.api || ""))];
  return writeHistory();
}

function pickLinks(document) {
  return {
    docxUrl: document.docx_url || "",
    structuredDocxUrl: document.structured_docx_url || "",
    pdfUrl: document.pdf_url || "",
    structuredPdfUrl: document.structured_pdf_url || "",
    sourceImageUrl: document.source_image_url || "",
  };
}

async function convertImages(fileList) {
  const files = [...fileList];
  if (updatePreparing.value) updatePreparing.value = false;
  if (!files.length || state.busy || updateReloadRequested.value) return;
  const api = state.api;
  let succeeded = 0;
  let historySaved = true;
  const failures = [];
  const fidelityNotices = [];
  const failedFiles = [];
  let documentToOpen = null;
  failedImageFiles = [];
  state.failedImageCount = 0;
  state.importStatus = "";
  setBusy(true, "正在准备图片", "正在确认当前接口的上传限制…");
  try {
    const maxUploadBytes = uploadLimitCheckedFor === api
      ? state.maxUploadBytes
      : await checkApi(api);
    if (state.api !== api) return;
    if (state.connectionStatus === "outdated") {
      state.importStatus = `已取消导入 0 / ${files.length} 张图片：图片识别服务版本较旧，文件尚未上传。请更新服务器后刷新网页再试。`;
      showToast("图片识别服务版本较旧，请更新服务器后重试");
      return;
    }
    const maxSourceImageBytes = Math.max(maxUploadBytes, state.maxSourceImageBytes);
    setBusy(true, "正在识别图片", "正在分析文字、标题和表格结构，请稍候…");
    for (const [index, file] of files.entries()) {
      const position = `${index + 1} / ${files.length}`;
      setBusy(true, `正在准备图片 ${position}`, `保留原图色彩和版式 · ${file.name}`);
      let failureStage = "本地准备";
      try {
        if (!isImageFile(file) || !file.size) throw new Error("请选择有效的图片文件");
        const sourceCanBeKept = canUploadImageUnchanged(file, maxSourceImageBytes);
        const prepared = await prepareImage(
          file,
          fileTitle(file.name),
          globalThis,
          maxUploadBytes,
          maxSourceImageBytes,
        );
        const form = new FormData();
        form.append("image", prepared, prepared.name);
        failureStage = "服务端识别";
        setBusy(true, `正在上传图片 ${position}`, `图片已准备好 · ${file.name}`);
        const document = await retryAfterRequest(
          () => requestDocument("/api/v1/documents/convert", {
            method: "POST",
            body: form,
            onUploadProgress: ({ loaded, total, lengthComputable }) => {
              if (!lengthComputable || total <= 0) {
                setBusy(true, `正在上传图片 ${position}`, file.name);
                return;
              }
              const percentage = Math.min(100, Math.round((loaded / total) * 100));
              setBusy(
                true,
                percentage === 100 ? `正在识别图片 ${position}` : `正在上传图片 ${position}`,
                percentage === 100
                  ? `${file.name} · 上传完成，正在分析文字和表格…`
                  : `${file.name} · ${percentage}%`,
              );
            },
          }, api),
          {
            onRetry: (seconds, error) => {
              const reason = error?.code === "OCR_NULL_CLOSE"
                ? "识别服务短暂中断"
                : error?.code === "OCR_TRANSIENT"
                  ? "自建 OCR 服务暂时不可用"
                  : "服务请求较忙";
              setBusy(
                true,
                `等待识别 ${position}`,
                `${file.name} · ${reason}，${seconds} 秒后自动重试…`,
              );
            },
            shouldRetryWithoutCooldown: error => (
              error?.code === "OCR_NULL_CLOSE"
              || error?.code === "OCR_TRANSIENT"
              || /Cannot read properties of null \(reading ['\"]close['\"]\)/i.test(String(error?.message || ""))
            ),
            maxRetriesWithoutCooldown: 1,
            fallbackWaitMilliseconds: 1000,
          },
        );
        if (!sourceCanBeKept && prepared.type === "image/png") {
          fidelityNotices.push(
            `${file.name}：已将浏览器解码后的图片保存为 PNG 编辑底图，避免 JPEG 二次压缩，请确认显示效果`,
          );
        } else if (!sourceCanBeKept) {
          fidelityNotices.push(
            `${file.name}：编辑背景已使用 JPEG 转换版，颜色或细节可能与原图不同，请核对`,
          );
        }
        const item = {
          id: document.id,
          api,
          title: resolveImageDocumentTitle(document.title || file.name, document),
          type: "image",
          hasTables: Boolean(document.has_tables),
          createdAt: Date.now(),
          thumb: document.source_image_url,
          ...pickLinks(document),
        };
        historySaved = addHistory(item) && historySaved;
        state.current = { ...document, title: item.title };
        succeeded += 1;
      } catch (error) {
        failures.push(formatImageImportFailure(file.name, failureStage, error));
        failedFiles.push(file);
      }
    }
    failedImageFiles = failedFiles;
    state.failedImageCount = failedFiles.length;
    if (succeeded) documentToOpen = state.current;
    const summary = `已完成 ${succeeded} / ${files.length} 张图片`;
    state.importStatus = [
      summary,
      ...fidelityNotices,
      ...failures,
      ...(!historySaved ? ["浏览器未能保存历史，请及时下载生成的文件。"] : []),
    ].join("；");
    showToast(failures.length ? `${summary}，失败原因见导入提示` : historySaved ? "识别完成，文档已加入历史" : "识别完成，请及时下载文件");
  } finally {
    setBusy(false);
  }
  if (documentToOpen) await openEditor(documentToOpen);
}

function retryFailedImages() {
  if (state.busy || failedImageFiles.length === 0) return;
  const files = [...failedImageFiles];
  failedImageFiles = [];
  state.failedImageCount = 0;
  void convertImages(files);
}

async function fetchDocument(id, api = state.api) {
  return requestDocument(
    `/api/v1/documents/${encodeURIComponent(id)}/refresh`,
    { cache: "no-store" },
    api,
  );
}

function findHistoryItem(id, api) {
  return state.history.find(item => item.id === id && (api === undefined || (item.api || "") === api));
}

function updateImageHistoryTitle(id, api, title) {
  const item = findHistoryItem(id, api);
  if (!item || item.type !== "image" || !title || item.title === title) return;
  item.title = title;
  writeHistory();
}

async function removeHistory(item) {
  if (state.busy || !item) return;
  setBusy(true, "正在删除", "正在移除本浏览器中的记录…");
  const previousHistory = state.history;
  const nextHistory = state.history.filter(entry => item.id !== entry.id || (item.api || "") !== (entry.api || ""));
  try {
    await commitHistoryBeforeFileMutation({
      persistNextHistory: () => {
        state.history = nextHistory;
        return writeHistory();
      },
      restorePreviousHistory: () => {
        state.history = previousHistory;
        return writeHistory();
      },
      mutateFiles: () => item.type === "pdf" && item.storage === "indexeddb"
        ? LocalDocuments.remove(item.id)
        : undefined,
    });
    state.sessionPdfs.delete(item.id);
    if (state.pdfId === item.id) clearPdfPreview();
    showToast(item.type === "pdf" ? "已删除记录与浏览器中的 PDF 副本" : "已从浏览器历史中删除");
  } catch (error) {
    showToast(`删除失败：${friendlyError(error)}`);
  } finally {
    setBusy(false);
  }
}

async function openHistory(item, event) {
  if (state.busy || !item) return;
  const latest = findHistoryItem(item.id, item.api);
  if (!latest) return;
  setBusy(true, "正在打开文档", "正在读取最新的编辑内容…");
  try {
    if (latest.type === "pdf") {
      await openPdfItem(latest, event?.currentTarget);
      return;
    }
    setBusy(false);
    await openEditor({ id: latest.id, title: latest.title || "扫描文档", api: latest.api });
  } catch (error) {
    showToast(`打开失败：${friendlyError(error)}`);
  } finally {
    setBusy(false);
  }
}

async function getPdfFile(item) {
  if (state.pdfId === item.id && state.pdfFile) return state.pdfFile;
  if (state.sessionPdfs.has(item.id)) return state.sessionPdfs.get(item.id);
  const record = await LocalDocuments.get(item.id);
  if (!record?.blob) throw new Error("这份 PDF 的本地副本不存在，请重新导入原文件");
  return new File([record.blob], record.name || `${item.title}.pdf`, {
    type: "application/pdf", lastModified: record.lastModified || Date.now(),
  });
}

async function openPdfItem(item, returnFocus = null) {
  const file = await getPdfFile(item);
  renderPdf(item, file, returnFocus);
  switchTool("pdf");
}

async function openPdfToolHistory(item, event) {
  if (state.busy || !item) return;
  setBusy(true, "正在打开 PDF", "正在读取浏览器中保存的文件…");
  try {
    await openPdfItem(item, event?.currentTarget);
  } catch (error) {
    showToast(`打开失败：${friendlyError(error)}`);
  } finally {
    setBusy(false);
  }
}

function renderPdf(item, file, returnFocus = null) {
  if (!state.pdfWorkspaceOpen) {
    pdfEditorReturnFocus = returnFocus?.isConnected
      ? returnFocus
      : pdfImportButton.value || pdfToolNavButton.value;
  }
  state.pdfId = item.id;
  state.pdfFile = file;
  state.pdfWorkspaceOpen = true;
  state.pdfNotice = item.sessionOnly
    ? "此文件仅在本次会话可用，刷新前请下载保存。"
    : "PDF 已保存在当前浏览器。清除浏览器数据会移除副本，请保留原文件。";
  window.document.body.classList.add("editor-open");
}

function clearPdfPreview() {
  state.pdfFile = null;
  state.pdfId = null;
  state.pdfWorkspaceOpen = false;
  state.pdfNotice = "";
  window.document.body.classList.remove("editor-open");
}

function closePdfEditor() {
  const returnFocus = pdfEditorReturnFocus;
  pdfEditorReturnFocus = null;
  clearPdfPreview();
  void nextTick(() => {
    const target = returnFocus?.isConnected
      ? returnFocus
      : pdfImportButton.value || pdfToolNavButton.value;
    focusElement(target);
  });
}

async function handlePdfFile(file, returnFocus = pdfImportButton.value) {
  if (updatePreparing.value) updatePreparing.value = false;
  if (state.busy || updateReloadRequested.value) return;
  if (!file || (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf"))) {
    showToast("请选择 PDF 文件");
    return;
  }
  if (!file.size) {
    showToast("PDF 文件为空，请重新选择");
    return;
  }
  setBusy(true, "正在导入 PDF", "正在保存到当前浏览器，文件不会上传…");
  try {
    const header = new TextDecoder().decode(await file.slice(0, 1024).arrayBuffer());
    if (!header.includes("%PDF-")) throw new Error("文件不是有效的 PDF，请检查文件内容");
    const item = {
      id: `pdf-${createId()}`,
      title: fileTitle(file.name),
      name: file.name,
      type: "pdf",
      createdAt: Date.now(),
      size: file.size,
    };
    let storageError = "";
    try {
      await LocalDocuments.put(item.id, file);
      item.storage = "indexeddb";
    } catch (error) {
      item.sessionOnly = true;
      state.sessionPdfs.set(item.id, file);
      storageError = friendlyError(error);
    }
    if (!addHistory(item)) {
      item.sessionOnly = true;
      state.sessionPdfs.set(item.id, file);
      storageError = "浏览器无法保存历史记录";
      if (item.storage === "indexeddb") {
        try {
          await LocalDocuments.remove(item.id);
          delete item.storage;
        } catch { /* Keep the imported file available in this session. */ }
      }
    }
    renderPdf(item, file, returnFocus);
    if (storageError) state.pdfNotice = `${storageError}。此文件仅在本次会话可用，刷新前请下载保存。`;
    switchTool("pdf");
  } catch (error) {
    showToast(`导入失败：${friendlyError(error)}`);
  } finally {
    setBusy(false);
  }
}

async function saveEditedPdfCopy(file) {
  if (!file || state.busy || !state.pdfFile) return;
  setBusy(true, "正在保存 PDF 副本", "正在将编辑版保存到本地历史，原文件会保留…");
  const item = {
    id: `pdf-${createId()}`,
    title: fileTitle(file.name),
    name: file.name,
    type: "pdf",
    createdAt: Date.now(),
    size: file.size,
  };
  const previousHistory = state.history;
  try {
    await LocalDocuments.put(item.id, file);
    item.storage = "indexeddb";
    if (!addHistory(item)) {
      state.history = previousHistory;
      await LocalDocuments.remove(item.id);
      throw new Error("浏览器无法保存历史记录");
    }
    renderPdf(item, file);
    showToast("编辑版已另存为新 PDF，原文件仍保留在历史中");
  } catch (error) {
    showToast(`保存失败：${friendlyError(error)}；可以点击“下载 PDF”保存到设备`);
  } finally {
    setBusy(false);
  }
}

async function shareLocalPdf() {
  const file = state.pdfFile;
  if (!file || state.busy) return;
  if (pdfEditor.value) {
    await pdfEditor.value.sharePdf();
    return;
  }
  try {
    if (navigator.share && navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file], title: fileTitle(file.name), text: "来自 DocuCraft 的 PDF 文档" });
    } else {
      downloadBlob(file, file.name || "文档.pdf", document);
      showToast("浏览器不支持直接分享，已下载 PDF，可发送该文件");
    }
  } catch (error) {
    if (error?.name !== "AbortError") showToast("分享未完成，可点击“下载 PDF”保存后发送");
  }
}

async function shareDocument(document = state.current) {
  if (!document) return;
  const link = document.pdf_url || document.docx_url || window.location.href;
  const data = { title: document.title || "扫描文档", text: "来自 DocuCraft 网页版的文档", url: link };
  const result = await shareLinkWithFallback({
    url: link,
    share: typeof navigator.share === "function" ? () => navigator.share(data) : null,
    copy: copyTextToClipboard,
    manualCopy: openManualCopy,
  });
  if (result === "copied") showToast("分享链接已复制");
}

async function openManualCopy(text) {
  if (!shareCopyDialog.value) {
    showToast("自动复制失败，请重新选择链接并按 Ctrl+C 复制");
    return;
  }
  await shareCopyDialog.value.open(text);
}

async function shareHistoryItem(item) {
  if (!item || state.busy) return;
  if (item.type === "pdf" && state.pdfId === item.id && state.pdfFile) {
    await shareLocalPdf();
    return;
  }
  setBusy(true, "正在准备分享", "正在读取文件…");
  try {
    if (item.type === "pdf") {
      await openPdfItem(item);
      showToast("PDF 已打开，请点击右上方的“分享”发送文件");
    } else {
      const document = await fetchDocument(item.id, item.api);
      await shareDocument({ ...document, title: item.title });
    }
  } catch (error) {
    showToast(`分享失败：${friendlyError(error)}`);
  } finally {
    setBusy(false);
  }
}

async function clearHistory() {
  if (state.busy || !state.history.length) return;
  if (!confirm("清空历史并移除当前浏览器保存的 PDF 副本？设备上的原文件和后端文档不会删除。")) return;
  setBusy(true, "正在清空历史", "正在移除当前浏览器中的记录与 PDF 副本…");
  const previousHistory = state.history;
  const ids = state.history.filter(item => item.type === "pdf" && item.storage === "indexeddb").map(item => item.id);
  try {
    await commitHistoryBeforeFileMutation({
      persistNextHistory: () => {
        state.history = [];
        return writeHistory();
      },
      restorePreviousHistory: () => {
        state.history = previousHistory;
        return writeHistory();
      },
      mutateFiles: () => ids.length ? LocalDocuments.removeMany(ids) : undefined,
    });
    state.sessionPdfs.clear();
    clearPdfPreview();
    showToast("已清空历史与本地 PDF 副本");
  } catch (error) {
    showToast(`清空失败：${friendlyError(error)}`);
  } finally {
    setBusy(false);
  }
}

function openSettings() {
  if (state.busy) return;
  apiDraft.value = state.api;
  apiError.value = "";
  settingsDialog.value?.showModal();
}

function closeSettings() {
  settingsDialog.value?.close();
}

function saveSettings() {
  let api;
  try {
    const url = new URL(normalizeApi(apiDraft.value) || DEFAULT_API);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error("invalid");
    api = normalizeApi(url.href);
  } catch {
    apiError.value = "请输入完整的 http:// 或 https:// 接口地址";
    apiInput.value?.setCustomValidity(apiError.value);
    apiInput.value?.reportValidity();
    return;
  }
  writeHistory();
  state.api = api;
  state.maxUploadBytes = FALLBACK_UPLOAD_LIMIT_BYTES;
  state.maxSourceImageBytes = FALLBACK_UPLOAD_LIMIT_BYTES;
  uploadLimitCheckedFor = "";
  let saved = true;
  try { localStorage.setItem(API_KEY, state.api); } catch { saved = false; }
  closeSettings();
  checkApi();
  showToast(saved ? "接口地址已保存，已有文档继续使用原接口" : "接口已切换，仅在本次会话有效");
}

async function openEditor(document = state.current) {
  if (state.busy) return;
  let editableDocument = document;
  if (!editableDocument?.editor_html && editableDocument?.id) {
    setBusy(true, "正在准备编辑器", "正在读取可编辑内容…");
    try {
      const refreshed = await fetchDocument(editableDocument.id, editableDocument.api || state.api);
      const title = resolveImageDocumentTitle(
        editableDocument.title || refreshed.title,
        refreshed,
      );
      editableDocument = { ...refreshed, title, api: editableDocument.api || refreshed.api };
      if (state.current?.id === editableDocument.id) state.current = editableDocument;
      updateImageHistoryTitle(editableDocument.id, editableDocument.api, title);
    } catch (error) {
      showToast(`无法打开编辑器：${friendlyError(error)}`);
      return;
    } finally {
      setBusy(false);
    }
  }
  if (editableDocument?.title) {
    const title = resolveImageDocumentTitle(editableDocument.title, editableDocument);
    if (title !== editableDocument.title) {
      editableDocument = { ...editableDocument, title };
      if (state.current?.id === editableDocument.id) state.current = editableDocument;
      updateImageHistoryTitle(editableDocument.id, editableDocument.api, title);
    }
  }
  if (!editableDocument?.editor_html) {
    showToast("当前文档没有可编辑内容");
    return;
  }
  editorReturnFocus = window.document.activeElement;
  state.editorBaseline = null;
  state.editorDirty = false;
  state.editorSrcdoc = prepareImageEditorWorkspaceHtml(editableDocument.editor_html);
  state.editorDocument = editableDocument;
  window.document.body.classList.add("editor-open");
  nextTick(() => focusElement(closeEditorButton.value));
}

async function onEditorLoad() {
  const current = state.editorDocument;
  if (!current || !editorFrame.value) return;
  try {
    const baseline = await requestEditorDocument(editorFrame.value);
    if (state.editorDocument === current) state.editorBaseline = baseline;
  } catch {
    if (state.editorDocument === current) state.editorBaseline = null;
  }
}

async function editorHasChanges() {
  const current = state.editorDocument;
  if (!current) return false;
  try {
    const snapshot = await requestEditorDocument(editorFrame.value);
    if (state.editorDocument !== current) return false;
    if (state.editorBaseline === null) {
      state.editorBaseline = snapshot;
      return state.editorDirty;
    }
    return snapshot !== state.editorBaseline || state.editorDirty;
  }
  catch { return true; }
}

function reflowPlainTextParagraph(value) {
  return value
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map(line => line.trim())
    .filter(Boolean)
    .reduce((paragraph, line) => {
      if (!paragraph) return line;
      // OCR may put several list items inside one editor paragraph. Keep
      // their boundaries while still joining ordinary wrapped prose lines.
      if (/^(?:[-*•·▪◦‣]\s+|\d{1,3}[.)、）]\s*|[（(]\d{1,3}[）)]\s*|[一二三四五六七八九十]+[、.．]\s*|\[[ xX]\]\s+)/.test(line)) {
        return `${paragraph}\n${line}`;
      }
      const startsAsciiWord = /^[A-Za-z0-9]/.test(line);
      const previousCharacter = paragraph[paragraph.length - 1];
      const needsSpace = startsAsciiWord && (
        /[A-Za-z0-9]$/.test(previousCharacter)
        || /[,.;:!?]$/.test(previousCharacter)
      );
      return `${paragraph}${needsSpace ? " " : ""}${line}`;
    }, "");
}

function mergeCoordinateWordEdits(paragraphs, current, positionedWords) {
  const originalParagraphs = Array.isArray(current?.paragraphs) ? current.paragraphs : [];
  const originalWords = Array.isArray(current?.positioned_words) ? current.positioned_words : [];
  if (!Array.isArray(positionedWords) || !originalWords.length) return paragraphs;

  const editsByParagraph = new Map();
  positionedWords.forEach((word, index) => {
    if (word?.text_paragraph_index === null || word?.text_paragraph_index === undefined) return;
    if (word?.source_index === null) return;
    const paragraphIndex = Number(word.text_paragraph_index);
    const sourceIndex = word?.source_index === undefined ? index : Number(word.source_index);
    if (!Number.isInteger(sourceIndex)) return;
    if (!Number.isInteger(paragraphIndex) || paragraphIndex < 0 || paragraphIndex >= paragraphs.length) return;
    if (sourceIndex < 0 || sourceIndex >= originalWords.length) return;

    const originalText = typeof originalWords[sourceIndex]?.text === "string"
      ? originalWords[sourceIndex].text
      : "";
    const editedText = typeof word?.text === "string" ? word.text.replace(/\r\n?/g, "\n").replace(/\n/g, "") : "";
    if (!editsByParagraph.has(paragraphIndex)) editsByParagraph.set(paragraphIndex, []);
    editsByParagraph.get(paragraphIndex).push({ sourceIndex, originalText, editedText });
  });

  const mergedParagraphs = [...paragraphs];
  for (const [paragraphIndex, edits] of editsByParagraph) {
    const originalParagraph = originalParagraphs[paragraphIndex];
    if (typeof originalParagraph !== "string" || paragraphs[paragraphIndex] !== originalParagraph) continue;

    let cursor = 0;
    let replacementFailed = false;
    const replacements = [];
    edits.sort((left, right) => left.sourceIndex - right.sourceIndex);
    for (const edit of edits) {
      if (!edit.originalText) continue;
      const start = originalParagraph.indexOf(edit.originalText, cursor);
      if (start < 0) {
        replacementFailed = true;
        break;
      }
      const end = start + edit.originalText.length;
      cursor = end;
      if (edit.originalText !== edit.editedText) {
        replacements.push({ start, end, text: edit.editedText });
      }
    }
    if (replacementFailed) continue;
    let merged = originalParagraph;
    for (let index = replacements.length - 1; index >= 0; index -= 1) {
      const replacement = replacements[index];
      merged = `${merged.slice(0, replacement.start)}${replacement.text}${merged.slice(replacement.end)}`;
    }
    mergedParagraphs[paragraphIndex] = merged;
  }
  return mergedParagraphs;
}

function mergeCoordinateWordEditsIntoTables(tables, tableOriginals, current, positionedWords) {
  const sourceTables = Array.isArray(current?.table_cells) ? current.table_cells : [];
  const originalWords = Array.isArray(current?.positioned_words) ? current.positioned_words : [];
  if (!Array.isArray(positionedWords) || !Array.isArray(tableOriginals) || !originalWords.length) return tables;

  const editedWordsBySource = new Map();
  positionedWords.forEach(word => {
    if (word?.source_index === null || word?.source_index === undefined) return;
    const sourceIndex = Number(word.source_index);
    if (Number.isInteger(sourceIndex) && sourceIndex >= 0) editedWordsBySource.set(sourceIndex, word);
  });

  const mergedTables = tables.map(table => table.map(row => Array.isArray(row) ? [...row] : row));
  sourceTables.forEach((cells, tableIndex) => {
    const table = mergedTables[tableIndex];
    const originals = tableOriginals[tableIndex];
    if (!Array.isArray(cells) || !Array.isArray(table) || !Array.isArray(originals)) return;

    cells.forEach(cell => {
      const rowIndex = Number(cell?.row_start);
      const columnIndex = Number(cell?.col_start);
      if (!Number.isInteger(rowIndex) || !Number.isInteger(columnIndex) || rowIndex < 0 || columnIndex < 0) return;
      if (!Array.isArray(table[rowIndex]) || !Array.isArray(originals[rowIndex])) return;
      const originalCellText = originals[rowIndex][columnIndex];
      const currentCellText = table[rowIndex][columnIndex];
      if (typeof originalCellText !== "string" || currentCellText !== originalCellText) return;

      const left = Number(cell.left);
      const top = Number(cell.top);
      const right = Number(cell.right);
      const bottom = Number(cell.bottom);
      if (![left, top, right, bottom].every(Number.isFinite) || right <= left || bottom <= top) return;

      const wordsInCell = originalWords
        .map((word, sourceIndex) => {
          const wordLeft = Number(word?.left);
          const wordTop = Number(word?.top);
          const wordWidth = Number(word?.width);
          const wordHeight = Number(word?.height);
          const centerX = wordLeft + wordWidth / 2;
          const centerY = wordTop + wordHeight / 2;
          if (![wordLeft, wordTop, wordWidth, wordHeight].every(Number.isFinite)) return null;
          if (centerX < left || centerX > right || centerY < top || centerY > bottom) return null;
          const editedWord = editedWordsBySource.get(sourceIndex);
          if (!editedWord) return null;
          const originalText = typeof word.text === "string" ? word.text : "";
          const editedText = typeof editedWord.text === "string"
            ? editedWord.text.replace(/\r\n?/g, "\n").replace(/\n/g, "")
            : "";
          return { sourceIndex, originalText, editedText, top: wordTop, left: wordLeft };
        })
        .filter(Boolean)
        .sort((first, second) => first.top - second.top || first.left - second.left || first.sourceIndex - second.sourceIndex);
      if (!wordsInCell.some(word => word.originalText !== word.editedText)) return;

      let cursor = 0;
      let replacementFailed = false;
      const replacements = [];
      for (const word of wordsInCell) {
        if (!word.originalText) continue;
        const start = originalCellText.indexOf(word.originalText, cursor);
        if (start < 0) {
          replacementFailed = true;
          break;
        }
        const end = start + word.originalText.length;
        cursor = end;
        if (word.originalText !== word.editedText) replacements.push({ start, end, text: word.editedText });
      }
      if (replacementFailed) return;

      let merged = originalCellText;
      for (let index = replacements.length - 1; index >= 0; index -= 1) {
        const replacement = replacements[index];
        merged = `${merged.slice(0, replacement.start)}${replacement.text}${merged.slice(replacement.end)}`;
      }
      table[rowIndex][columnIndex] = merged;
    });
  });
  return mergedTables;
}

function coordinateWordsOutsideTables(current, positionedWords) {
  if (!Array.isArray(positionedWords)) return [];
  const normalizeLine = value => String(value ?? "").replace(/\s+/g, "").trim();
  const paragraphLines = new Set((Array.isArray(current?.paragraphs) ? current.paragraphs : [])
    .flatMap(value => String(value ?? "").split(/\r\n?|\n/))
    .map(normalizeLine)
    .filter(Boolean));
  const tableCells = (Array.isArray(current?.table_cells) ? current.table_cells : [])
    .flatMap(table => Array.isArray(table) ? table : [])
    .filter(cell => {
      const bounds = [cell?.left, cell?.top, cell?.right, cell?.bottom].map(Number);
      return bounds.every(Number.isFinite) && bounds[2] > bounds[0] && bounds[3] > bounds[1];
    });
  const words = positionedWords.map((word, sourceOrder) => {
    const paragraphIndex = word?.text_paragraph_index;
    const text = typeof word?.text === "string"
      ? word.text.replace(/\r\n?/g, " ").replace(/\n/g, " ").replace(/\s+/g, " ").trim()
      : "";
    const left = Number(word?.left);
    const top = Number(word?.top);
    const width = Number(word?.width);
    const height = Number(word?.height);
    if (paragraphIndex !== null && paragraphIndex !== undefined) return null;
    if (!text || ![left, top, width, height].every(Number.isFinite) || width < 0 || height < 0) return null;
    const centerX = left + width / 2;
    const centerY = top + height / 2;
    if (tableCells.some(cell => {
      const epsilon = Math.max(1, Math.min(width, height) * 0.05);
      return centerX >= Number(cell.left) - epsilon && centerX <= Number(cell.right) + epsilon
        && centerY >= Number(cell.top) - epsilon && centerY <= Number(cell.bottom) + epsilon;
    })) return null;
    return { text, left, top, width, height, sourceOrder };
  }).filter(Boolean).sort((first, second) =>
    first.top - second.top || first.left - second.left || first.sourceOrder - second.sourceOrder,
  );

  const lines = [];
  for (const word of words) {
    let line = lines[lines.length - 1];
    if (!line || Math.abs(word.top - line.top) > Math.max(line.height * 0.65, word.height * 0.65, 8)) {
      line = { top: word.top, height: word.height, words: [] };
      lines.push(line);
    } else {
      line.height = Math.max(line.height, word.height);
    }
    line.words.push(word);
  }

  return lines.map(line => {
    let text = "";
    let previousWord = null;
    let previousToken = "";
    for (const word of line.words) {
      const token = word.text;
      if (text) {
        const previousCharacter = text[text.length - 1];
        const currentCharacter = token[0];
        const isAsciiAlphanumeric = character => character?.codePointAt(0) < 128 && /[A-Za-z0-9]/.test(character);
        const asciiPair = isAsciiAlphanumeric(previousCharacter) && isAsciiAlphanumeric(currentCharacter);
        const previousHeight = Math.max(previousWord?.height || 0, 1);
        const currentHeight = Math.max(word.height, 1);
        const previousWidth = previousWord?.width || 0;
        const gap = word.left - ((previousWord?.left || 0) + previousWidth);
        const typicalHeight = Math.min(previousHeight, currentHeight);
        const contiguousCharacterBoxes = asciiPair && previousToken.length === 1 && token.length === 1
          && previousWidth > 0 && word.width > 0
          && previousWidth <= typicalHeight * 1.4
          && word.width <= typicalHeight * 1.4
          && gap <= Math.max(typicalHeight * 0.16, 1);
        if (asciiPair && !contiguousCharacterBoxes) {
          text += " ";
        } else if (isAsciiAlphanumeric(currentCharacter) && ",.;:!?".includes(previousCharacter)) {
          const characterBeforePunctuation = text[text.length - 2];
          const continuesNumber = ",.:".includes(previousCharacter)
            && /[0-9]/.test(characterBeforePunctuation || "")
            && /[0-9]/.test(currentCharacter);
          if (!continuesNumber) text += " ";
        }
      }
      text += token;
      previousWord = word;
      previousToken = token;
    }
    const normalizedText = normalizeLine(text);
    return { text: text.trim(), normalizedText, top: line.top };
  }).filter(line => line.text && !paragraphLines.has(line.normalizedText));
}

function sourceTopForExportBlock(block, current, positionedWords) {
  const index = Number(block?.index);
  if (!Number.isInteger(index) || index < 0) return Number.NaN;
  if (block.type === "paragraph") {
    const tops = (Array.isArray(positionedWords) ? positionedWords : [])
      .filter(word => Number(word?.text_paragraph_index) === index)
      .map(word => Number(word?.top))
      .filter(Number.isFinite);
    return tops.length ? Math.min(...tops) : Number.NaN;
  }
  if (block.type === "table") {
    const cells = current?.table_cells?.[index];
    const tops = (Array.isArray(cells) ? cells : [])
      .map(cell => Number(cell?.top))
      .filter(Number.isFinite);
    return tops.length ? Math.min(...tops) : Number.NaN;
  }
  return Number.NaN;
}

function joinPlainTextExportBlocks(blocks) {
  let text = "";
  let previousWasCoordinate = false;
  for (const block of blocks) {
    const content = typeof block?.text === "string" ? block.text.trim() : "";
    if (!content) continue;
    if (text) text += previousWasCoordinate && block.coordinate ? "\n" : "\n\n";
    text += content;
    previousWasCoordinate = Boolean(block.coordinate);
  }
  return text;
}

function insertCoordinateExportLines(blocks, coordinateLines) {
  for (const line of coordinateLines) {
    const insertionIndex = blocks.findIndex(block => Number.isFinite(block.top) && block.top > line.top);
    const entry = { text: line.text, coordinate: true, top: line.top };
    if (insertionIndex < 0) blocks.push(entry);
    else blocks.splice(insertionIndex, 0, entry);
  }
  return blocks;
}

async function readPlainTextEditorContent(current) {
  const frame = editorFrame.value;
  if (!frame?.contentWindow) throw new Error("editor_loading");
  const snapshot = await requestEditorDocument(frame);
  if (state.editorDocument !== current) return null;
  const payload = JSON.parse(snapshot);
  let rawParagraphs = Array.isArray(payload.paragraphs)
    ? payload.paragraphs.map(value => typeof value === "string" ? value : "")
    : [];
  rawParagraphs = mergeCoordinateWordEdits(rawParagraphs, current, payload.positioned_words);
  const paragraphs = rawParagraphs
    .map(value => reflowPlainTextParagraph(value));
  let rawTables = Array.isArray(payload.tables)
    ? payload.tables.map(table => Array.isArray(table)
      ? table.map(row => Array.isArray(row) ? row.map(cell => String(cell ?? "")) : [])
      : [])
    : [];
  rawTables = mergeCoordinateWordEditsIntoTables(
    rawTables,
    payload.table_originals,
    current,
    payload.positioned_words,
  );
  const tables = rawTables.map(table => {
    const rows = table
      .filter(row => Array.isArray(row))
      .map(row => row.map(cell => String(cell ?? "")
        .replace(/\r\n?/g, "\n")
        .replace(/\t/g, " ")
        .split("\n")
        .map(line => line.trim())
        .filter(Boolean)
        .join(" / "))
        .join("\t"));
    return rows.some(row => row.replace(/\t/g, "").trim()) ? rows.join("\n") : "";
  });
  const coordinateLines = coordinateWordsOutsideTables(current, payload.positioned_words);
  if (Array.isArray(payload.blocks)) {
    const orderedBlocks = payload.blocks.map(block => {
      const index = Number(block?.index);
      const text = !Number.isInteger(index) || index < 0
        ? ""
        : block.type === "paragraph" ? paragraphs[index] || ""
          : block.type === "table" ? tables[index] || ""
            : "";
      return { text, coordinate: false, top: sourceTopForExportBlock(block, current, payload.positioned_words) };
    }).filter(block => block.text);
    insertCoordinateExportLines(orderedBlocks, coordinateLines);
    return joinPlainTextExportBlocks(orderedBlocks);
  }
  const legacyBlocks = [
    ...paragraphs.map((text, index) => ({
      text,
      coordinate: false,
      top: sourceTopForExportBlock({ type: "paragraph", index }, current, payload.positioned_words),
    })),
    ...tables.map((text, index) => ({
      text,
      coordinate: false,
      top: sourceTopForExportBlock({ type: "table", index }, current, payload.positioned_words),
    })),
  ].filter(block => block.text);
  if (legacyBlocks.length && legacyBlocks.every(block => Number.isFinite(block.top))) {
    legacyBlocks.sort((first, second) => first.top - second.top);
  }
  insertCoordinateExportLines(legacyBlocks, coordinateLines);
  return joinPlainTextExportBlocks(legacyBlocks);
}

async function downloadPlainText() {
  const current = state.editorDocument;
  if (!current) return;
  try {
    const text = await readPlainTextEditorContent(current);
    if (text === null) return;
    if (!text) {
      showToast("没有可下载的识别文字");
      return;
    }
    const blob = new Blob(["\uFEFF", text], { type: "text/plain;charset=utf-8" });
    const filename = safeDownloadStem(current.title, "识别文字");
    downloadBlob(blob, `${filename}.txt`, window.document);
    showToast("纯文字已下载");
  } catch (error) {
    showToast(error?.message === "editor_loading" ? "编辑器还在加载，请稍候" : "读取识别文字失败，请重试");
  }
}

async function copyPlainText() {
  const current = state.editorDocument;
  if (!current) return;
  try {
    const text = await readPlainTextEditorContent(current);
    if (text === null) return;
    if (!text) {
      showToast("没有可复制的识别文字");
      return;
    }
    try {
      await copyTextToClipboard(text);
    } catch {
      const opened = await plainTextCopyDialog.value?.open(text);
      if (!opened) throw new Error("clipboard_unavailable");
      return;
    }
    showToast("纯文字已复制");
  } catch (error) {
    showToast(error?.message === "editor_loading" ? "编辑器还在加载，请稍候" : "复制失败，请重试");
  }
}

function onEditorMessage(event) {
  if (!isEditorFrameMessage(event, editorFrame.value)) return;
  if (event.data?.type === "iscan:editor-dirty") state.editorDirty = Boolean(event.data.dirty);
}

async function closeEditor(discard = false) {
  if (state.saving || state.busy) return;
  if (!discard && await editorHasChanges() && !confirm("修改尚未保存，确定返回并放弃这些修改吗？")) return;
  state.editorDocument = null;
  state.editorBaseline = null;
  state.editorDirty = false;
  state.editorSrcdoc = "";
  window.document.body.classList.remove("editor-open");
  await nextTick();
  focusElement(editorReturnFocus);
  editorReturnFocus = null;
}

async function saveEditor() {
  if (state.saving || state.busy || !state.editorDocument) return;
  const original = state.editorDocument;
  const frame = editorFrame.value;
  if (!frame?.contentWindow) {
    showToast("编辑器还在加载，请稍候");
    return;
  }
  state.saving = true;
  setBusy(true, "正在保存修改", "正在读取编辑内容…");
  let payload;
  let updatedDocument = null;
  try {
    let snapshot;
    try {
      snapshot = await requestEditorDocument(frame);
      payload = JSON.parse(snapshot);
    } catch {
      showToast("读取编辑内容失败，请重试");
      return;
    }
    if (state.editorDocument !== original) return;
    const hasChanges = state.editorDirty
      || (state.editorBaseline !== null && snapshot !== state.editorBaseline);
    if (state.editorBaseline === null) state.editorBaseline = snapshot;
    if (!hasChanges) {
      showToast("没有未保存的修改");
      return;
    }
    state.busyText = "正在重新生成 Word 和 PDF…";
    const updated = await requestDocument("/api/v1/documents/export-edited", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        document_id: original.id,
        title: original.title || "扫描文档",
        paragraphs: payload.paragraphs || [],
        tables: payload.tables || [],
        positioned_words: payload.positioned_words || [],
      }),
    }, original.api || state.api);
    updated.title = original.title;
    const saved = addHistory({
      id: updated.id,
      api: updated.api,
      title: updated.title,
      type: "image",
      hasTables: Boolean(updated.has_tables),
      createdAt: Date.now(),
      thumb: updated.source_image_url,
      ...pickLinks(updated),
    });
    state.current = updated;
    updatedDocument = updated;
    showToast(saved ? "已保存，新的文件已加入历史" : "文件已生成，但浏览器无法保存历史，请立即下载");
  } catch (error) {
    showToast(`保存失败：${friendlyError(error)}`);
  } finally {
    state.saving = false;
    setBusy(false);
    if (updatedDocument?.editor_html) {
      try {
        const updatedSrcdoc = prepareImageEditorWorkspaceHtml(updatedDocument.editor_html);
        state.editorBaseline = null;
        state.editorDirty = false;
        state.editorDocument = updatedDocument;
        state.editorSrcdoc = updatedSrcdoc;
      } catch {
        await closeEditor(true);
        showToast("修改已保存，但新编辑内容暂时无法加载；可从历史文档重新打开");
      }
    } else if (updatedDocument) {
      await closeEditor(true);
      showToast("修改已保存；请从历史文档重新打开查看新版本");
    }
  }
}

function carriesFiles(event) {
  return Array.from(event.dataTransfer?.types || []).includes("Files");
}

function onDragEnter(kind, event) {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  dragging[kind] = true;
}

function onDragOver(kind, event) {
  if (!carriesFiles(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
  dragging[kind] = true;
}

function onDragLeave(kind, event) {
  if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return;
  dragging[kind] = false;
}

function onImportDrop(kind, event) {
  dragging[kind] = false;
  if (!carriesFiles(event)) return;
  event.preventDefault();
  const files = Array.from(event.dataTransfer.files || []);
  if (!files.length) return;
  if (kind === "image") {
    if (files.some(file => !isImageFile(file))) {
      showToast("图片识别只接收图片文件");
      return;
    }
    convertImages(files);
    return;
  }
  if (files.length !== 1) {
    showToast("一次只能导入一个 PDF 文件");
    return;
  }
  const file = files[0];
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    showToast("请将 PDF 文件拖入预览区域");
    return;
  }
  handlePdfFile(file);
}

function onImageChange(event) {
  filePickerOpen.value = false;
  const files = [...event.target.files];
  event.target.value = "";
  convertImages(files);
}

function onPdfChange(event) {
  filePickerOpen.value = false;
  const file = event.target.files[0];
  event.target.value = "";
  if (file) handlePdfFile(file, pdfImportButton.value);
}

function openPdfPicker() {
  pdfInputSecondary.value?.click();
}

function openImagePicker() {
  imageInput.value?.click();
}

function markFilePickerOpen() {
  filePickerOpen.value = true;
}

function clearFilePickerOpen() {
  filePickerOpen.value = false;
}

function preventFileNavigation(event) {
  if (carriesFiles(event)) event.preventDefault();
}

onMounted(() => {
  window.document.addEventListener("dragover", preventFileNavigation);
  window.document.addEventListener("drop", preventFileNavigation);
  window.addEventListener("message", onEditorMessage);
  beforeUnloadHandler = event => {
    if (state.saving || state.editorDirty || (pdfEditor.value?.hasEdits && !updateReloadRequested.value)) {
      event.preventDefault();
      event.returnValue = "";
    }
  };
  window.addEventListener("beforeunload", beforeUnloadHandler);
  checkApi();
  window.addEventListener("iscan:assistant-stream-state", onAssistantStreamState);
  if (import.meta.env.PROD) {
    checkForAppUpdateWhenVisible();
    appUpdateTimer = window.setInterval(checkForAppUpdateWhenVisible, 60_000);
    window.document.addEventListener("visibilitychange", checkForAppUpdateWhenVisible);
    window.addEventListener("online", checkForAppUpdateWhenOnline);
  }

  if (window.isSecureContext && "serviceWorker" in navigator) {
    const updatingControlledPage = Boolean(navigator.serviceWorker.controller);
    if (updatingControlledPage) {
      navigator.serviceWorker.addEventListener("controllerchange", onServiceWorkerControllerChange, { once: true });
      serviceWorkerControllerChangeListening = true;
    }
    navigator.serviceWorker.register("./service-worker.js", { updateViaCache: "none" })
      .then(registration => registration.update().catch(() => {}))
      .catch(() => {});
  }
});

onBeforeUnmount(() => {
  window.document.body.classList.remove("editor-open");
  window.document.removeEventListener("dragover", preventFileNavigation);
  window.document.removeEventListener("drop", preventFileNavigation);
  window.removeEventListener("message", onEditorMessage);
  if (beforeUnloadHandler) window.removeEventListener("beforeunload", beforeUnloadHandler);
  window.removeEventListener("iscan:assistant-stream-state", onAssistantStreamState);
  window.clearInterval(appUpdateTimer);
  window.document.removeEventListener("visibilitychange", checkForAppUpdateWhenVisible);
  window.removeEventListener("online", checkForAppUpdateWhenOnline);
  if (serviceWorkerControllerChangeListening && "serviceWorker" in navigator) {
    navigator.serviceWorker.removeEventListener("controllerchange", onServiceWorkerControllerChange);
  }
  window.clearTimeout(toastTimer);
  window.document.body.classList.remove("editor-open");
  failedImageFiles = [];
});
</script>

<template>
  <div class="app-shell" :inert="!assistantState.ready || state.busy || Boolean(state.editorDocument) || state.pdfWorkspaceOpen || updatePreparing || updateReloadRequested">
    <aside class="sidebar" aria-label="主导航">
      <img class="brand-mark" :src="logoUrl" alt="DocuCraft" width="48" height="48" />
      <div class="brand-copy">
        <strong>DocuCraft</strong>
        <span>AI 写作与文档工作台</span>
      </div>
      <button class="new-chat-button" type="button" :disabled="assistantState.localLoadFailed" :title="assistantState.localLoadFailed ? '本地对话存储不可用' : undefined" @click="startAssistantConversation"><svg class="new-chat-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12h14" /></svg><span>新建对话</span></button>

      <nav class="nav-list">
        <button class="nav-item mobile-new-conversation-button" type="button" aria-label="新建对话" title="新建对话" :disabled="assistantState.localLoadFailed" @click="startAssistantConversation">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5v14M5 12h14" /></svg><span>新对话</span>
        </button>
        <button class="nav-item mobile-conversations-button" type="button" aria-label="对话记录" :title="assistantState.localLoadFailed ? '本地对话存储不可用' : '对话记录'" :disabled="assistantState.localLoadFailed" @click="openConversationHistory">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 5.5h16v11H9l-5 4v-4z" /><path d="M8 9.5h8M8 12.5h5" /></svg><span>对话</span>
        </button>
        <span class="nav-group-label">项目工具</span>
        <button class="nav-item" :class="{ 'is-active': state.view === 'tools' && state.activeTool === 'image' }" :aria-current="state.view === 'tools' && state.activeTool === 'image' ? 'page' : undefined" aria-label="图片识别" title="图片识别" @click="switchTool('image')">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="9" r="1.5" /><path d="m4 19 5-6 3 3 3-4 5 7" /></svg><span>图片识别</span>
        </button>
        <button ref="pdfToolNavButton" class="nav-item" :class="{ 'is-active': state.view === 'tools' && state.activeTool === 'pdf' }" :aria-current="state.view === 'tools' && state.activeTool === 'pdf' ? 'page' : undefined" aria-label="PDF 编辑器" title="PDF 编辑器" @click="switchTool('pdf')">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v5h5M9 12h6M9 16h6" /></svg><span>PDF 编辑器</span>
        </button>
        <button class="nav-item" :class="{ 'is-active': state.view === 'history' }" :aria-current="state.view === 'history' ? 'page' : undefined" aria-label="历史文档" title="历史文档" @click="openAllHistory">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7a9 9 0 1 1-1 5" /><path d="M4 3v4h4M12 7v5l3 2" /></svg><span>历史文档</span>
        </button>
        <button class="nav-item compact-settings-button" type="button" aria-label="接口设置" title="接口设置" aria-haspopup="dialog" aria-controls="settingsDialog" @click="openSettings">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="17" r="2" /></svg><span>设置</span>
        </button>
      </nav>

      <div v-if="!assistantState.localLoadFailed && recentAssistantConversations.length" class="assistant-sidebar-history">
        <div class="assistant-sidebar-history-heading">
          <p>最近对话</p>
          <button type="button" aria-label="查看全部对话" @click="openConversationHistory">全部</button>
        </div>
        <div class="assistant-sidebar-history-list">
          <div v-for="conversation in recentAssistantConversations" :key="conversation.id" class="assistant-sidebar-row">
            <button class="assistant-sidebar-conversation" :class="{ 'is-active': assistantState.activeId === conversation.id && state.view === 'assistant' }" type="button" :title="conversation.title" @click="openAssistantConversation(conversation.id)">{{ conversation.title }}</button>
            <button class="assistant-sidebar-delete" type="button" :disabled="isConversationGenerating(conversation.id)" :aria-label="isConversationGenerating(conversation.id) ? `生成完成后可删除：${conversation.title}` : `删除对话：${conversation.title}`" :title="isConversationGenerating(conversation.id) ? '生成完成后可删除' : '删除对话'" @click="deleteAssistantConversation(conversation.id)"><svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3" /></svg></button>
          </div>
        </div>
      </div>

      <div class="sidebar-spacer"></div>
      <div class="connection-card">
        <span class="status-dot" :class="{ 'is-online': state.connectionStatus === 'online', 'is-offline': state.connectionStatus === 'offline', 'is-outdated': state.connectionStatus === 'outdated' }"></span>
        <div>
          <strong>{{ state.connectionMessage }}</strong>
          <span>{{ state.connectionStatus === 'outdated' ? '请更新服务器以匹配此网页版本' : '可在设置中切换接口' }}</span>
        </div>
      </div>
      <button class="settings-link" type="button" aria-label="接口设置" title="接口设置" aria-haspopup="dialog" aria-controls="settingsDialog" @click="openSettings"><svg class="nav-icon settings-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="17" r="2" /></svg><span>接口设置</span></button>
    </aside>

    <main class="main-area" :class="{ 'is-assistant-main': state.view === 'assistant' }" :aria-busy="state.busy">
      <header v-if="state.view === 'tools'" class="topbar tool-page-header">
        <h1>{{ state.activeTool === 'image' ? '图片识别' : 'PDF 编辑器' }}</h1>
        <button v-if="state.activeTool === 'image'" ref="imageImportButton" class="primary-button small-button" type="button" aria-controls="imageInput" @click="openImagePicker">选择图片</button>
        <input v-if="state.activeTool === 'image'" ref="imageInput" id="imageInput" class="visually-hidden" type="file" accept="image/*" multiple tabindex="-1" @click="markFilePickerOpen" @cancel="clearFilePickerOpen" @change="onImageChange" />
        <button v-if="state.activeTool === 'pdf'" ref="pdfImportButton" class="primary-button small-button" type="button" aria-controls="pdfInputSecondary" @click="openPdfPicker">导入 PDF</button>
        <input v-if="state.activeTool === 'pdf'" ref="pdfInputSecondary" id="pdfInputSecondary" class="visually-hidden" type="file" accept="application/pdf,.pdf" tabindex="-1" @click="markFilePickerOpen" @cancel="clearFilePickerOpen" @change="onPdfChange" />
      </header>

      <section v-show="state.view === 'assistant'" class="view-panel assistant-view" :class="{ 'is-visible': state.view === 'assistant' }">
        <div v-if="assistantState.localLoadFailed" class="assistant-storage-error" role="alert">
          <div class="assistant-storage-error-card">
            <strong>AI 对话暂不可用</strong>
            <p>浏览器暂时无法读取此设备的对话数据库。请关闭其他 DocuCraft 标签页后重试；为避免覆盖旧记录，AI 对话和文档编辑已暂停。图片识别、历史文件和 PDF 编辑器仍可使用。</p>
            <button class="primary-button" type="button" @click="retryAssistantStorage">重新读取</button>
          </div>
        </div>
        <AssistantWorkspace v-else :api-base="state.api" :active="state.view === 'assistant'" />
      </section>

      <section v-if="state.view === 'tools' && state.activeTool === 'image'" class="view-panel is-visible tool-image-content" :class="{ 'is-dragging': dragging.image }"
        @dragenter="onDragEnter('image', $event)" @dragover="onDragOver('image', $event)"
        @dragleave="onDragLeave('image', $event)" @drop.stop.prevent="onImportDrop('image', $event)">
        <p class="pdf-tool-description image-tool-description">识别图片中的文字、标题和表格，生成可编辑文档。导入后可直接校对内容，并保留原图供版式参照。</p>
        <p v-if="state.importStatus" class="import-status" role="status" aria-live="polite" aria-atomic="true">{{ state.importStatus }}</p>
        <button v-if="state.failedImageCount" class="import-retry-button" type="button" :disabled="state.busy" @click="retryFailedImages">重试失败图片（{{ state.failedImageCount }}）</button>
        <div class="pdf-empty image-upload-empty" role="status">
          <FileIcon class="empty-icon" kind="image" />
          <h3>还没有打开图片</h3>
          <p>选择或拖入图片后，即可识别内容并进入可编辑页面。</p>
        </div>

        <section class="recent-section">
          <div class="section-heading">
            <div><span class="section-kicker">最近使用</span><h3>识别历史</h3></div>
            <button class="text-button" @click="openToolHistory('image')">全部历史 →</button>
          </div>
          <div class="history-list compact">
            <article v-for="item in recentImageHistory" :key="`${item.id}:${item.api || ''}`" class="history-card">
              <div class="history-thumb"><img v-if="item.thumb" :src="item.thumb" alt="" loading="lazy" decoding="async" /><FileIcon v-else class="history-kind-icon" :kind="historyIcon(item)" /></div>
              <div class="history-info"><strong :title="item.title">{{ item.title }}</strong><span>{{ historySubtitle(item) }}</span></div>
              <div class="history-actions">
                <button :disabled="state.busy" title="打开" @click="openHistory(item, $event)">打开</button>
                <button :disabled="state.busy" title="分享" @click="shareHistoryItem(item)">分享</button>
                <button :disabled="state.busy" title="删除" @click="removeHistory(item)">删除</button>
              </div>
            </article>
            <div v-if="!recentImageHistory.length" class="empty-state"><FileIcon class="empty-icon" /><div><h3>还没有识别记录</h3><p>识别图片后，文档会显示在这里。</p></div></div>
          </div>
        </section>
      </section>

      <section v-if="state.view === 'history'" class="view-panel is-visible">
        <div class="section-heading large-heading">
          <div>
            <span class="section-kicker">LOCAL HISTORY</span>
            <h1>历史文档</h1>
            <p>图片识别文档保存在对应后端；导入的 PDF 保存在本浏览器。清除浏览器数据会移除本地历史和 PDF，请及时下载重要文件。</p>
          </div>
          <button class="danger-text-button" :disabled="state.busy || !state.history.length" @click="clearHistory">清空历史</button>
        </div>
        <div class="history-controls">
          <label class="history-search-wrap" for="historySearch">
            <svg class="history-search-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="10.8" cy="10.8" r="6.3" /><path d="m15.5 15.5 4 4" /></svg>
            <input id="historySearch" v-model="state.historySearch" class="history-search" type="search" placeholder="搜索文档名称" autocomplete="off" />
          </label>
          <div class="history-filter-list" role="group" aria-label="筛选文档类型">
            <button v-for="filter in [{ id: 'all', label: '全部' }, { id: 'image', label: '图片识别' }, { id: 'pdf', label: 'PDF' }]"
              :key="filter.id" class="history-filter" :class="{ 'is-active': state.historyFilter === filter.id }"
              type="button" :aria-pressed="state.historyFilter === filter.id" @click="state.historyFilter = filter.id">{{ filter.label }}</button>
          </div>
        </div>
        <p class="history-count" aria-live="polite">显示 {{ visibleHistory.length }} / {{ matchingHistory.length }} 份文档</p>
        <div id="history-list" class="history-list">
          <article v-for="item in visibleHistory" :key="`${item.id}:${item.api || ''}`" class="history-card">
            <div class="history-thumb"><img v-if="item.thumb" :src="item.thumb" alt="" loading="lazy" decoding="async" /><FileIcon v-else class="history-kind-icon" :kind="historyIcon(item)" /></div>
            <div class="history-info"><strong :title="item.title">{{ item.title }}</strong><span>{{ historySubtitle(item) }}</span></div>
            <div class="history-actions">
              <button :disabled="state.busy" title="打开" @click="openHistory(item, $event)">打开</button>
              <button :disabled="state.busy" title="分享" @click="shareHistoryItem(item)">分享</button>
              <button :disabled="state.busy" title="删除" @click="removeHistory(item)">删除</button>
            </div>
          </article>
          <div v-if="!matchingHistory.length" class="empty-state"><FileIcon class="empty-icon" /><div><h3>{{ historyEmptyTitle }}</h3><p>{{ historyEmptyText }}</p></div></div>
        </div>
        <button v-if="hasMoreHistory" class="history-load-more" type="button" aria-controls="history-list" @click="loadMoreHistory">加载更多（剩余 {{ matchingHistory.length - visibleHistory.length }} 份）</button>
      </section>

      <section v-if="state.view === 'tools' && state.activeTool === 'pdf'" class="view-panel is-visible tool-pdf-content" :class="{ 'is-dragging': dragging.pdf }"
        @dragenter="onDragEnter('pdf', $event)" @dragover="onDragOver('pdf', $event)"
        @dragleave="onDragLeave('pdf', $event)" @drop.stop.prevent="onImportDrop('pdf', $event)">
        <p class="pdf-tool-description">支持页面预览、擦除文字、添加图片或手写签名、保存副本和下载。文件只保存在当前浏览器，不会上传；原文件不会被覆盖。</p>
        <p v-if="state.pdfNotice" class="pdf-storage-notice" role="status" aria-live="polite" aria-atomic="true">{{ state.pdfNotice }}</p>
        <div v-if="!state.pdfFile" class="pdf-empty">
          <FileIcon class="empty-icon" kind="pdf" /><h3>还没有打开 PDF</h3><p>选择或拖入 PDF 后，即可预览并使用编辑工具。</p>
        </div>
        <section class="recent-section">
          <div class="section-heading">
            <div><span class="section-kicker">最近使用</span><h3>PDF 历史</h3></div>
            <button class="text-button" @click="openToolHistory('pdf')">全部历史 →</button>
          </div>
          <div class="history-list compact">
            <article v-for="item in recentPdfHistory" :key="item.id" class="history-card">
              <div class="history-thumb"><FileIcon class="history-kind-icon" :kind="historyIcon(item)" /></div>
              <div class="history-info"><strong :title="item.title">{{ item.title }}</strong><span>{{ historySubtitle(item) }}</span></div>
              <div class="history-actions">
                <button :disabled="state.busy" title="打开 PDF 编辑" @click="openPdfToolHistory(item, $event)">打开</button>
                <button :disabled="state.busy" title="分享" @click="shareHistoryItem(item)">分享</button>
                <button :disabled="state.busy" title="删除" @click="removeHistory(item)">删除</button>
              </div>
            </article>
            <div v-if="!recentPdfHistory.length" class="empty-state"><FileIcon class="empty-icon" kind="pdf" /><div><h3>还没有 PDF 记录</h3><p>导入 PDF 后，文件会显示在这里。</p></div></div>
          </div>
        </section>
      </section>

    </main>
  </div>

  <section v-if="state.busy" class="busy-overlay" role="status" aria-live="polite" aria-atomic="true">
    <div class="busy-card"><div class="loader" aria-hidden="true"></div><strong>{{ state.busyTitle }}</strong><span>{{ state.busyText }}</span></div>
  </section>

  <aside v-if="hasAppUpdate && !state.editorDocument && !state.pdfWorkspaceOpen" class="app-update-notice" role="status" aria-live="polite">
    <div class="app-update-copy">
      <strong>DocuCraft 有新版本</strong>
      <span>{{ updateReloadRequested ? (assistantStreamBusy ? "当前生成结束并保存后会自动刷新。" : "正在切换到新版本…") : updatePreparing ? "正在保存数据并确认当前操作…" : "刷新页面即可使用最新功能。" }}</span>
    </div>
    <button class="app-update-action" type="button" :disabled="updatePreparing || updateReloadRequested" @click="applyAppUpdate">{{ updateReloadRequested ? "等待中" : updatePreparing ? "准备中…" : "刷新页面" }}</button>
    <button v-if="!updateReloadRequested" class="app-update-dismiss" type="button" :aria-label="updatePreparing ? '取消更新' : '稍后更新'" @click="dismissAppUpdate">{{ updatePreparing ? "取消更新" : "稍后" }}</button>
  </aside>

  <dialog id="settingsDialog" ref="settingsDialog" class="settings-dialog" aria-labelledby="settingsDialogTitle">
    <form @submit.prevent="saveSettings">
      <div class="dialog-heading"><div><span class="section-kicker">CONNECTION</span><h2 id="settingsDialogTitle">接口设置</h2></div><button class="icon-button" type="button" aria-label="关闭" @click="closeSettings"><svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg></button></div>
      <label class="field-label" for="apiInput">后端地址</label>
      <input id="apiInput" ref="apiInput" v-model="apiDraft" class="text-input" inputmode="url" placeholder="http://127.0.0.1:5000" :aria-invalid="Boolean(apiError)" @input="apiError = ''; apiInput?.setCustomValidity('')" />
      <p class="field-help">例如：<code>http://localhost:5000</code> 或 <code>https://api.example.com</code>。如果网页通过 HTTPS 打开，后端也需要使用 HTTPS，浏览器才允许上传。</p>
      <div class="dialog-actions"><button class="ghost-button" type="button" @click="closeSettings">取消</button><button class="primary-button" type="submit">保存并检查</button></div>
    </form>
  </dialog>

  <ManualCopyDialog ref="shareCopyDialog" title="复制分享链接" description="链接已选中，请按 Ctrl+C（Mac 按 ⌘+C）复制。" label="要复制的分享链接" select-button-label="重新选择链接" />
  <ManualCopyDialog ref="plainTextCopyDialog" title="复制识别文字" description="识别文字已选中，请按 Ctrl+C（Mac 按 ⌘+C）复制。" label="要复制的识别文字" select-button-label="重新选择文字" />

  <dialog ref="conversationDialog" class="conversation-dialog" aria-labelledby="conversationDialogTitle" @close="conversationDialogOpen = false">
    <div class="conversation-dialog-heading">
      <div><span class="section-kicker">ALL CHATS</span><h2 id="conversationDialogTitle">对话记录</h2></div>
      <button class="icon-button" type="button" aria-label="关闭对话记录" @click="closeConversationHistory"><svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg></button>
    </div>
    <div v-if="conversationDialogOpen && !assistantState.localLoadFailed && visibleAssistantConversations.length" class="conversation-dialog-list">
      <div v-for="conversation in visibleAssistantConversations" :key="conversation.id" class="conversation-dialog-row">
        <button class="conversation-dialog-open" :class="{ 'is-active': assistantState.activeId === conversation.id && state.view === 'assistant' }" type="button" @click="openConversationFromHistory(conversation.id)">
          <strong>{{ conversation.title || "新对话" }}</strong>
          <span>{{ conversation.messages?.at(-1)?.content?.slice(0, 72) || "打开这条对话" }}</span>
        </button>
        <button class="assistant-sidebar-delete" type="button" :disabled="isConversationGenerating(conversation.id)" :aria-label="isConversationGenerating(conversation.id) ? `生成完成后可删除：${conversation.title}` : `删除对话：${conversation.title}`" :title="isConversationGenerating(conversation.id) ? '生成完成后可删除' : '删除对话'" @click="deleteAssistantConversation(conversation.id)"><svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3" /></svg></button>
      </div>
    </div>
    <div v-else-if="conversationDialogOpen" class="conversation-dialog-empty">
      <strong>还没有对话记录</strong>
      <span>开始一条新对话后，会保存在这里。</span>
      <button class="primary-button" type="button" :disabled="assistantState.localLoadFailed" @click="startAssistantConversation(); closeConversationHistory()">新建对话</button>
    </div>
    <button v-if="conversationDialogOpen && hasMoreAssistantConversations" class="conversation-dialog-more" type="button" @click="loadMoreAssistantConversations">加载更多对话（剩余 {{ assistantConversationHistory.length - visibleAssistantConversations.length }} 条）</button>
  </dialog>

  <div v-if="state.editorDocument" class="editor-modal" role="dialog" aria-modal="true" aria-label="编辑文档" :aria-busy="state.busy" :inert="state.busy">
    <DocumentWorkspace class="image-document-workspace" :title="state.editorDocument.title || '识别文档'">
      <template #actions>
        <div class="image-editor-actions">
          <button class="ghost-button" :disabled="state.busy || state.saving" title="表格以制表符分列" @click="copyPlainText">复制纯文字</button>
          <button class="ghost-button" :disabled="state.busy || state.saving" title="表格以制表符分列" @click="downloadPlainText">下载纯文字</button>
          <a v-if="state.editorDocument.docx_url" class="primary-button image-editor-download" :href="state.editorDocument.docx_url">{{ state.editorDocument.docx_preserves_source_layout ? "下载 Word（原图版式）" : "下载 Word" }}</a>
          <a v-if="state.editorDocument.structured_docx_url" class="ghost-button image-editor-download" :href="state.editorDocument.structured_docx_url">{{ state.editorDocument.has_tables ? "下载 Word（可编辑表格）" : "下载 Word（可编辑文字）" }}</a>
          <a v-if="state.editorDocument.pdf_url" class="ghost-button image-editor-download" :href="state.editorDocument.pdf_url">{{ state.editorDocument.has_tables ? "下载 PDF（原图版式）" : "下载 PDF" }}{{ !state.editorDocument.has_tables && state.editorDocument.pdf_preserves_source_layout ? "（原图版式）" : "" }}</a>
          <a v-if="state.editorDocument.structured_pdf_url" class="ghost-button image-editor-download" :href="state.editorDocument.structured_pdf_url">下载 PDF（可复制文字）</a>
          <button class="ghost-button" :disabled="state.busy" @click="shareDocument(state.editorDocument)">分享</button>
          <button class="primary-button" :disabled="state.busy" @click="saveEditor">{{ state.saving ? "正在保存…" : "保存修改" }}</button>
          <button ref="closeEditorButton" class="icon-button document-workspace-close" type="button" aria-label="关闭编辑器" title="关闭" :disabled="state.busy" @click="closeEditor">
            <svg class="toolbar-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
      </template>
      <p v-if="state.editorDocument.warnings?.length" class="image-editor-warning" role="status">{{ state.editorDocument.warnings.join("；") }}</p>
      <iframe ref="editorFrame" class="editor-frame" :srcdoc="state.editorSrcdoc" title="可编辑文档" sandbox="allow-scripts" @load="onEditorLoad"></iframe>
    </DocumentWorkspace>
  </div>

  <div v-if="state.pdfWorkspaceOpen && state.pdfFile" ref="pdfEditorModal" class="editor-modal pdf-editor-modal" role="dialog" aria-modal="true" aria-label="PDF 编辑器" tabindex="-1" :aria-busy="state.busy" :inert="state.busy">
    <PdfEditor ref="pdfEditor" :file="state.pdfFile" :active="true" @toast="showToast" @save-copy="saveEditedPdfCopy" @close="closePdfEditor" />
  </div>

  <div class="toast" :class="{ 'is-visible': state.toastVisible }" role="status" aria-live="polite" aria-atomic="true">{{ state.toast }}</div>

  <div v-if="!assistantState.ready" class="assistant-storage-gate" role="status" aria-live="polite">
    <div class="assistant-storage-gate-card">
      <div class="loader" aria-hidden="true"></div>
      <strong>正在读取本地对话…</strong>
    </div>
  </div>
</template>
