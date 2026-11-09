import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("keeps the document close control at the far right and uses font-independent toolbar icons", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const toolbar = workspace.match(/<header class="document-toolbar">([\s\S]*?)<\/header>/)?.[1] || "";
  const downloadAction = toolbar.indexOf("downloadMarkdown");
  const closeAction = toolbar.indexOf('class="document-tool-button document-close-button"');

  assert.notEqual(downloadAction, -1);
  assert.ok(closeAction > downloadAction, "close button should follow every document action");
  assert.match(toolbar, /aria-label="关闭文档区"[\s\S]*?<svg class="toolbar-icon"/);
  assert.match(toolbar, /<svg class="toolbar-icon"[\s\S]*?<path v-if="fullscreenTarget === 'document'"/);
  assert.match(toolbar, /toolbar-chevron-icon/);
  assert.match(toolbar, /toolbar-download-icon/);
  assert.doesNotMatch(toolbar, /[×⛶⤢⌄↓]/);
});

test("uses vector icons for assistant pane controls and document mode choices", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const assistantTools = workspace.match(/<div class="assistant-panel-tools">([\s\S]*?)<\/div>/)?.[1] || "";
  const documentMenu = workspace.match(/<div v-show="editorMenuOpen" id="document-mode-options" class="document-menu" role="group" aria-label="文档显示模式">([\s\S]*?)<\/div>/)?.[1] || "";
  const outlineToggle = workspace.match(/<button class="document-outline-toggle"[\s\S]*?<\/button>/)?.[0] || "";

  assert.match(assistantTools, /<svg v-if="documentPaneOpen" class="toolbar-icon"/);
  assert.match(assistantTools, /aria-label="交换文档与 AI 面板位置"[\s\S]*?<svg class="toolbar-icon"/);
  assert.doesNotMatch(assistantTools, /[◫▤⇄]/);
  assert.match(documentMenu, /class="menu-action-icon"/);
  assert.match(documentMenu, /class="menu-selected-icon"/);
  assert.doesNotMatch(documentMenu, /[✎◉✓]/);
  assert.match(outlineToggle, /class="outline-toggle-icon"/);
  assert.doesNotMatch(outlineToggle, /[»«]/);
});

test("exposes document toolbar popovers as keyboard-operable disclosures", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const toolbar = workspace.match(/<header class="document-toolbar">([\s\S]*?)<\/header>/)?.[1] || "";

  assert.match(toolbar, /aria-controls="document-mode-options" :aria-expanded="editorMenuOpen"/);
  assert.match(toolbar, /aria-controls="document-export-options" :aria-expanded="exportMenuOpen"/);
  assert.match(toolbar, /role="group" aria-label="文档显示模式"/);
  assert.match(toolbar, /role="group" aria-label="下载格式"/);
  assert.match(toolbar, /@keydown\.esc\.stop\.prevent="closeToolbarDisclosure\('editor', true\)"/);
  assert.match(toolbar, /@keydown\.esc\.stop\.prevent="closeToolbarDisclosure\('export', true\)"/);
  assert.doesNotMatch(toolbar, /aria-haspopup="menu"|role="menu"|role="menuitem"/);
  assert.match(workspace, /function closeToolbarDisclosure\(kind, returnFocus = false\)[\s\S]*?nextTick\(\(\) => trigger\.value\?\.focus\(\)\)/);
});
