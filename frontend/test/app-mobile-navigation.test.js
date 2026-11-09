import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createServer } from "vite";

test("renders an accessible mobile action for starting a new conversation", async () => {
  const previousWindow = globalThis.window;
  const previousIndexedDB = globalThis.indexedDB;
  globalThis.window = { location: { hostname: "127.0.0.1" } };
  globalThis.indexedDB = undefined;
  const vite = await createServer({ server: { middlewareMode: true, hmr: false } });

  try {
    const { default: App } = await vite.ssrLoadModule("/src/App.vue");
    const html = await renderToString(createSSRApp(App));

    assert.match(html, /class="nav-item mobile-new-conversation-button"[^>]*aria-label="新建对话"/);
    assert.match(html, /<svg class="nav-icon" viewBox="0 0 24 24"[^>]*><path d="M12 5v14M5 12h14"><\/path><\/svg><span>新对话<\/span>/);
    assert.match(html, /class="nav-item compact-settings-button"[^>]*aria-label="接口设置"[^>]*aria-haspopup="dialog"/);
    assert.match(html, /<dialog id="settingsDialog"[^>]*aria-labelledby="settingsDialogTitle"/);
    assert.match(html, /<h2 id="settingsDialogTitle">接口设置<\/h2>/);
    assert.doesNotMatch(html, /class="conversation-dialog-list"|class="conversation-dialog-empty"/, "closed conversation history should not render its rows");
    assert.doesNotMatch(html, /LOCAL HISTORY|从文件开始/, "inactive import and document-history pages should not render on the assistant home");
    const nav = html.match(/<nav class="nav-list">([\s\S]*?)<\/nav>/)?.[1] || "";
    assert.doesNotMatch(nav, /class="nav-group-label">工作台|aria-label="AI 助手"/, "the redundant workbench navigation group should be removed");
    assert.match(nav, /aria-label="新建对话"/);
    assert.match(nav, /aria-label="对话记录"/);
    assert.equal(html.match(/aria-current="page"/g)?.length || 0, 0, "the assistant view has no redundant sidebar destination");
  } finally {
    await vite.close();
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    if (previousIndexedDB === undefined) delete globalThis.indexedDB;
    else globalThis.indexedDB = previousIndexedDB;
  }
});

test("keeps only recent chats in the sidebar and pages full history inside the open dialog", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(app, /sliceAssistantConversationHistory\([\s\S]*?ASSISTANT_SIDEBAR_HISTORY_LIMIT/);
  assert.match(app, /v-for="conversation in recentAssistantConversations"/);
  assert.match(app, /v-if="conversationDialogOpen && !assistantState\.localLoadFailed && visibleAssistantConversations\.length"/);
  assert.match(app, /v-for="conversation in visibleAssistantConversations"/);
  assert.match(app, /v-if="conversationDialogOpen && hasMoreAssistantConversations"/);
  assert.match(app, /aria-label="查看全部对话"/);
});

test("keeps the mobile new-conversation action inside the mobile bottom navigation", () => {
  const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

  assert.match(styles, /\.mobile-new-conversation-button\s*\{\s*display:\s*none;\s*\}/);
  assert.match(
    styles,
    /@media\s*\(max-width:\s*620px\)[\s\S]*?\.mobile-new-conversation-button\s*\{\s*display:\s*flex;\s*\}/,
  );
});

test("shows one settings entry point per viewport layout", () => {
  const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const compactLayout = styles.match(/@media\s*\(max-width:\s*1024px\)\s*\{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(compactLayout, /\.settings-link,\s*\.assistant-sidebar-history\s*\{\s*display:\s*none;\s*\}/);
  assert.match(styles, /\.compact-settings-button\s*\{\s*display:\s*none;\s*\}/);
  assert.match(compactLayout, /\.compact-settings-button\s*\{\s*display:\s*flex;\s*\}/);
  assert.doesNotMatch(app, /class="topbar-actions"/);
});

test("isolates the document editor in a scripts-only sandbox", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  assert.match(app, /<iframe[^>]*sandbox="allow-scripts"/);
  assert.doesNotMatch(app, /sandbox="[^"]*allow-same-origin/);
  assert.match(app, /requestEditorDocument\(frame\)/);
});

test("uses one shared tool-page header without adding a duplicate brand bar", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

  assert.match(app, /<header v-if="state\.view === 'tools'" class="topbar tool-page-header">/);
  assert.match(app, /state\.activeTool === 'image' \? '图片识别' : 'PDF 编辑器'/);
  assert.doesNotMatch(app, /workspace-label|is-subpage/);
  assert.doesNotMatch(styles, /\.topbar\.is-subpage|\.workspace-label|\.topbar > div:first-child/);
});

test("gives history and PDF screens one page-level heading", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const historyPage = app.match(/<section v-if="state\.view === 'history'"[\s\S]*?<\/section>/)?.[0] || "";
  const toolHeader = app.match(/<header v-if="state\.view === 'tools'" class="topbar tool-page-header">[\s\S]*?<\/header>/)?.[0] || "";

  assert.match(historyPage, /<h1>历史文档<\/h1>/);
  assert.match(toolHeader, /state\.activeTool === 'image' \? '图片识别' : 'PDF 编辑器'/);
  assert.match(app, /class="view-panel is-visible tool-pdf-content"/);
});

test("keeps separate image and PDF entries inside one shared tools page", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const nav = app.match(/<nav class="nav-list">([\s\S]*?)<\/nav>/)?.[1] || "";

  assert.match(nav, /aria-label="图片识别"[^>]*@click="switchTool\('image'\)"/);
  assert.match(nav, /aria-label="PDF 编辑器"[^>]*@click="switchTool\('pdf'\)"/);
  assert.ok(nav.indexOf('aria-label="PDF 编辑器"') < nav.indexOf('aria-label="历史文档"'));
  assert.match(nav, /<svg class="nav-icon" viewBox="0 0 24 24"/);
  assert.doesNotMatch(nav, /[☷▧▤↺⚙＋]/);
  assert.doesNotMatch(nav, /图片识别和 PDF 编辑器/);
  assert.match(app, /function switchTool\(name\)[\s\S]*?state\.view = "tools"/);
  assert.match(app, /state\.view === 'tools' && state\.activeTool === 'image'/);
  assert.match(app, /state\.view === 'tools' && state\.activeTool === 'pdf'/);
  assert.match(app, /<button v-if="state\.activeTool === 'image'" ref="imageImportButton"[^>]*>选择图片<\/button>/);
  assert.match(app, /class="pdf-empty image-upload-empty"/);
  assert.match(app, /<button v-if="state\.activeTool === 'pdf'" ref="pdfImportButton" class="primary-button small-button" type="button" aria-controls="pdfInputSecondary" @click="openPdfPicker">导入 PDF<\/button>/);
  assert.match(app, /function openPdfPicker\(\)\s*\{\s*pdfInputSecondary\.value\?\.click\(\);\s*\}/);
  assert.match(app, /state\.pdfWorkspaceOpen && state\.pdfFile[\s\S]*?<PdfEditor ref="pdfEditor" :file="state\.pdfFile" :active="true"[^>]*@save-copy="saveEditedPdfCopy"/);
  assert.match(app, /<input v-if="state\.activeTool === 'pdf'" ref="pdfInputSecondary" id="pdfInputSecondary" class="visually-hidden" type="file" accept="application\/pdf,\.pdf" tabindex="-1" @click="markFilePickerOpen" @cancel="clearFilePickerOpen" @change="onPdfChange" \/>/);
  assert.match(app, /@drop\.stop\.prevent="onImportDrop\('pdf', \$event\)"/);
  assert.match(app, /支持页面预览、擦除文字、添加图片或手写签名、保存副本和下载/);
  assert.match(app, /recentImageHistory = computed\(\(\) => orderedHistory\.value\.filter\(item => item\.type === "image"\)/);
  assert.match(app, /recentPdfHistory = computed\(\(\) => orderedHistory\.value\.filter\(item => item\.type === "pdf"\)/);
  assert.match(app, /function openAllHistory\(\)[\s\S]*?state\.historyFilter = "all";[\s\S]*?state\.historySearch = ""/);
  assert.match(app, /function openToolHistory\(type\)[\s\S]*?type !== "image" && type !== "pdf"[\s\S]*?state\.historyFilter = type;[\s\S]*?state\.historySearch = ""/);
  assert.match(nav, /aria-label="历史文档"[^>]*@click="openAllHistory"/);
  assert.match(app, /识别历史<\/h3><\/div>\s*<button class="text-button" @click="openToolHistory\('image'\)">/);
  assert.match(app, /PDF 历史<\/h3><\/div>\s*<button class="text-button" @click="openToolHistory\('pdf'\)">/);
  assert.match(app, /v-for="item in recentImageHistory"/);
  assert.match(app, /v-for="item in recentPdfHistory"/);
  assert.match(app, /@click="openPdfToolHistory\(item, \$event\)"/);
});

test("mounts tool content and document history only while selected", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(app, /<section v-if="state\.view === 'tools' && state\.activeTool === 'image'" class="view-panel is-visible tool-image-content" :class=/);
  assert.match(app, /<section v-if="state\.view === 'history'" class="view-panel is-visible">/);
  assert.match(app, /<section v-show="state\.view === 'assistant'"/);
  assert.match(app, /<section v-if="state\.view === 'tools' && state\.activeTool === 'pdf'"/);
  assert.doesNotMatch(app, /state\.view === '(home|pdf)'/);
});

test("opens image history directly in the shared editable document workspace", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const pdfEditor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const openHistory = app.match(/async function openHistory\(item, event\)[\s\S]*?\n}\n/)?.[0] || "";
  const openEditor = app.match(/async function openEditor\(document = state\.current\)[\s\S]*?\n}\n/)?.[0] || "";

  assert.match(openHistory, /await openPdfItem\(latest, event\?\.currentTarget\)/);
  assert.match(openHistory, /await openEditor\(\{ id: latest\.id, title: latest\.title \|\| "扫描文档", api: latest\.api \}\)/);
  assert.match(app, /<DocumentWorkspace class="image-document-workspace" :title="state\.editorDocument\.title/);
  assert.match(pdfEditor, /<DocumentWorkspace class="pdf-editor" :title="file\.name"/);
  assert.doesNotMatch(app, /state\.view === 'viewer'|document-viewer|openDocumentViewer/);
  assert.match(openEditor, /!editableDocument\?\.editor_html && editableDocument\?\.id/);
  assert.match(openEditor, /fetchDocument\(editableDocument\.id, editableDocument\.api \|\| state\.api\)/);
  assert.match(openEditor, /prepareImageEditorWorkspaceHtml\(editableDocument\.editor_html\)/);
  assert.match(app, /prepareImageEditorWorkspaceHtml\(updatedDocument\.editor_html\)/);
});

test("opens newly recognized images directly in the editor with downloads available there", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const convertImages = app.match(/async function convertImages\(fileList\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const saveEditor = app.match(/async function saveEditor\(\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(convertImages, /if \(succeeded\) documentToOpen = state\.current/);
  assert.match(convertImages, /if \(documentToOpen\) await openEditor\(documentToOpen\)/);
  assert.doesNotMatch(app, /result-panel|RECOGNITION COMPLETE|class="action-button primary" @click="openEditor\(\)"/);
  assert.match(app, /class="primary-button image-editor-download" :href="state\.editorDocument\.docx_url"/);
  assert.match(app, /class="ghost-button image-editor-download" :href="state\.editorDocument\.pdf_url"/);
  assert.match(saveEditor, /state\.editorDocument = updatedDocument/);
  assert.match(saveEditor, /state\.editorSrcdoc = updatedSrcdoc/);
  assert.match(saveEditor, /state\.saving = true[\s\S]*?await requestEditorDocument\(frame\)/);
  assert.match(saveEditor, /snapshot !== state\.editorBaseline/);
  assert.match(saveEditor, /没有未保存的修改/);
  assert.match(app, /return snapshot !== state\.editorBaseline \|\| state\.editorDirty/);
});

test("surfaces backend warnings in the editable image document", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(
    app,
    /v-if="state\.editorDocument\.warnings\?\.length" class="image-editor-warning" role="status">\{\{ state\.editorDocument\.warnings\.join\("；"\) \}\}/,
  );
});

test("uses the active backend upload limit before preparing imported images", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const checkApi = app.match(/async function checkApi\(api = state\.api\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const checkApiFailure = checkApi.split("} catch {")[1]?.split("} finally {")[0] || "";
  const convertImages = app.match(/async function convertImages\(fileList\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const saveSettings = app.match(/function saveSettings\(\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(checkApi, /health\?\.max_upload_bytes/);
  assert.match(checkApi, /health\?\.max_source_image_bytes/);
  assert.match(checkApi, /isBackendVersionCompatible\([\s\S]*?__ISCAN_BACKEND_FINGERPRINT__/);
  assert.match(checkApi, /state\.connectionStatus = backendVersionCompatible \? "online" : "outdated"/);
  assert.match(checkApi, /state\.maxSourceImageBytes = maxSourceImageBytes/);
  assert.match(checkApi, /uploadLimitCheckedFor = api/);
  assert.doesNotMatch(checkApiFailure, /uploadLimitCheckedFor = api/);
  assert.match(saveSettings, /state\.maxUploadBytes = FALLBACK_UPLOAD_LIMIT_BYTES;[\s\S]*?state\.maxSourceImageBytes = FALLBACK_UPLOAD_LIMIT_BYTES;[\s\S]*?uploadLimitCheckedFor = ""/);
  assert.match(convertImages, /uploadLimitCheckedFor === api[\s\S]*?: await checkApi\(api\)/);
  assert.match(convertImages, /state\.api !== api/);
  assert.match(convertImages, /globalThis,[\s\S]*?maxUploadBytes,[\s\S]*?maxSourceImageBytes/);
  assert.match(convertImages, /canUploadImageUnchanged\(file, maxSourceImageBytes\)/);
  assert.match(convertImages, /编辑背景已使用 JPEG 转换版/);
});

test("retries legacy image close errors and gives coded errors a useful message", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const convertImages = app.match(/async function convertImages\(fileList\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const errorMessages = readFileSync(new URL("../src/error-messages.js", import.meta.url), "utf8");

  assert.match(convertImages, /shouldRetryWithoutCooldown:[\s\S]*?maxRetriesWithoutCooldown: 1/);
  assert.match(convertImages, /shouldRetryWithoutCooldown:[\s\S]*?error\?\.code === "OCR_NULL_CLOSE"[\s\S]*?Cannot read properties of null/);
  assert.doesNotMatch(convertImages, /shouldRetryWithoutCooldown:[\s\S]*?!error\?\.code/);
  assert.match(convertImages, /formatImageImportFailure\(file\.name, failureStage, error\)/);
  assert.match(errorMessages, /error\?\.code === "OCR_NULL_CLOSE"[\s\S]*图片识别服务暂时处理失败/);
  assert.match(errorMessages, /error\?\.requestId[\s\S]*图片识别服务暂时处理失败[\s\S]*浏览器暂时无法处理这张图片/);
});

test("does not upload images when the backend version is outdated", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const convertImages = app.match(/async function convertImages\(fileList\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const versionGuard = convertImages.indexOf('if (state.connectionStatus === "outdated")');
  const uploadLoop = convertImages.indexOf("for (const [index, file] of files.entries())");

  assert.notEqual(versionGuard, -1);
  assert.ok(versionGuard < uploadLoop, "the backend version check must run before image preparation and upload");
  assert.match(convertImages, /state\.importStatus = `已取消导入 0 \/ \$\{files\.length\} 张图片：图片识别服务版本较旧，文件尚未上传/);
  assert.match(convertImages, /请更新服务器后刷新网页再试/);
});
