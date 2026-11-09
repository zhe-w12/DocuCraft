import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("uses reusable vector file icons for history and empty states", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const fileIcon = readFileSync(new URL("../src/FileIcon.vue", import.meta.url), "utf8");

  assert.match(app, /function historyIcon\(item\) \{\s*return item\.type === "pdf" \? "pdf" : item\.hasTables \? "table" : "text";/);
  assert.match(app, /<FileIcon v-else class="history-kind-icon" :kind="historyIcon\(item\)" \/>/);
  assert.match(app, /<FileIcon class="empty-icon" kind="pdf" \/>/);
  assert.match(fileIcon, /kind === 'pdf'/);
  assert.match(fileIcon, /kind === 'table'/);
  assert.match(fileIcon, /kind === 'image'/);
  assert.match(fileIcon, /<svg class="file-icon" viewBox="0 0 24 24"/);
});

test("uses SVG for close, delete, search, and new-chat controls", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const pdfEditor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  const deleteControls = app.match(/<button class="assistant-sidebar-delete"[\s\S]*?<\/button>/g) || [];

  assert.match(app, /aria-label="关闭编辑器"[\s\S]*?<svg class="toolbar-icon"/);
  assert.match(app, /aria-label="关闭对话记录"[^>]*><svg class="toolbar-icon"/);
  assert.equal(deleteControls.length, 2);
  for (const control of deleteControls) assert.match(control, /<svg class="toolbar-icon"/);
  assert.match(app, /class="history-search-icon" viewBox="0 0 24 24"/);
  assert.match(app, /class="new-chat-icon" viewBox="0 0 24 24"/);
  assert.match(pdfEditor, /ref="drawDialogCloseButton"[\s\S]*?aria-label="关闭"[\s\S]*?><svg class="toolbar-icon"/);
  assert.doesNotMatch(app, /[×⌕▤▦▧]/);
  assert.doesNotMatch(pdfEditor, /aria-label="关闭"[^\n]*>×</);
  assert.doesNotMatch(styles, /\.icon-button[^\n]*font-size:/);
  assert.doesNotMatch(styles, /\.assistant-sidebar-delete[^\n]*font-size:/);
});
