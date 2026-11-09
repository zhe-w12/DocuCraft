import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

test("continues image batches after a server Retry-After response", () => {
  assert.match(app, /retryAfterRequest\([\s\S]*?requestDocument\("\/api\/v1\/documents\/convert"/);
  assert.match(app, /onRetry: \(seconds, error\) => \{[\s\S]*?error\?\.code === "OCR_NULL_CLOSE"[\s\S]*?error\?\.code === "OCR_TRANSIENT"[\s\S]*?秒后自动重试/);
  assert.match(app, /shouldRetryWithoutCooldown: error => \([\s\S]*?error\?\.code === "OCR_NULL_CLOSE"[\s\S]*?error\?\.code === "OCR_TRANSIENT"[\s\S]*?Cannot read properties of null/);
});

test("retains only failed image files for an explicit retry", () => {
  const retry = app.match(/function retryFailedImages\(\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(app, /failures\.push\([\s\S]*?failedFiles\.push\(file\)/);
  assert.match(app, /failedImageFiles = failedFiles;\s*state\.failedImageCount = failedFiles\.length/);
  assert.match(retry, /const files = \[\.\.\.failedImageFiles\]/);
  assert.match(retry, /void convertImages\(files\)/);
  assert.match(app, /class="import-retry-button"[^>]*@click="retryFailedImages"/);
  assert.match(styles, /@media \(max-width: 620px\)[\s\S]*?\.import-retry-button \{ width: 100%; min-height: 44px; \}/);
});

test("defaults image exports to scan fidelity and labels editable alternatives", () => {
  assert.match(app, /"structured_pdf_url"/);
  assert.match(app, /state\.editorDocument\.has_tables \? "下载 PDF（原图版式）" : "下载 PDF"/);
  assert.match(app, /state\.editorDocument\.structured_pdf_url[\s\S]*?下载 PDF（可复制文字）/);
  assert.match(app, /structuredPdfUrl: document\.structured_pdf_url \|\| ""/);
  assert.match(app, /v-if="state\.editorDocument\.docx_url" class="primary-button image-editor-download"[\s\S]*?下载 Word（原图版式）/);
  assert.match(app, /state\.editorDocument\.structured_docx_url" class="ghost-button image-editor-download"[\s\S]*?下载 Word（可编辑表格）/);
});

test("uses the PDF tool landing-page structure for image recognition", () => {
  const header = app.match(/<header v-if="state\.view === 'tools'" class="topbar tool-page-header">([\s\S]*?)<\/header>/)?.[1] || "";
  const imageTool = app.match(/<section v-if="state\.view === 'tools' && state\.activeTool === 'image'[\s\S]*?<section class="recent-section">([\s\S]*?)<\/section>/)?.[0] || "";

  assert.match(header, /图片识别/);
  assert.match(header, /ref="imageImportButton"[^>]*aria-controls="imageInput"[^>]*>选择图片/);
  assert.match(header, /ref="imageInput"[^>]*type="file" accept="image\/\*" multiple/);
  assert.match(imageTool, /识别图片中的文字、标题和表格/);
  assert.match(imageTool, /class="pdf-empty image-upload-empty"/);
  assert.match(imageTool, /选择或拖入图片后，即可识别内容并进入可编辑页面/);
  assert.match(imageTool, /v-for="item in recentImageHistory"/);
  assert.match(imageTool, /openToolHistory\('image'\)/);
  assert.doesNotMatch(imageTool, /class="import-card/);
  assert.match(app, /function openImagePicker\(\) \{\s*imageInput\.value\?\.click\(\);\s*\}/);
  assert.match(styles, /\.tool-image-content\.is-dragging::after[^{]*\{[^}]*松开以识别图片/);
  assert.match(styles, /\.image-upload-empty \{ min-height: 278px; \}/);
});
