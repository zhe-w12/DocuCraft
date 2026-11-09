import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createSSRApp } from "vue";
import { renderToString } from "@vue/server-renderer";
import { createServer } from "vite";

test("renders PDF editing tools when an imported file is provided", async () => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false } });
  try {
    const { default: PdfEditor } = await vite.ssrLoadModule("/src/PdfEditor.vue");
    const html = await renderToString(createSSRApp(PdfEditor, {
      file: { name: "sample.pdf", size: 2048 },
    }));

    assert.match(html, /class="pdf-editor-toolbar"/);
    for (const control of ["擦除文字", "添加图片签名", "手写签名", "保存副本", "下载 PDF"]) {
      assert.ok(html.includes(control), `Expected the imported-PDF view to contain ${control}`);
    }
    assert.match(html, /aria-label="关闭 PDF 编辑器"/);
    assert.match(html, /自动将浅色背景转透明/);
    assert.match(html, /aria-label="上一页"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
    assert.match(html, /aria-label="缩小"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
    assert.doesNotMatch(html, /class="pdf-editor-footer"/);
    assert.doesNotMatch(html, /恢复原文件/);
    assert.doesNotMatch(html, /[↶↻‹›]/);
  } finally {
    await vite.close();
  }
});

test("uses vector icons for PDF navigation, zoom, and signature rotation", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const toolbarStart = editor.indexOf('<div class="pdf-editor-toolbar"');
  const toolbarEnd = editor.indexOf("</template>", toolbarStart);
  const toolbar = toolbarStart >= 0 && toolbarEnd >= 0
    ? editor.slice(toolbarStart, toolbarEnd)
    : "";

  assert.match(toolbar, /aria-label="上一页"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
  assert.match(toolbar, /aria-label="下一页"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
  assert.match(toolbar, /aria-label="缩小"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
  assert.match(toolbar, /aria-label="放大"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
  assert.match(toolbar, /aria-label="逆时针旋转签名 90 度"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
  assert.match(toolbar, /aria-label="顺时针旋转签名 90 度"[^>]*><svg class="toolbar-icon pdf-control-icon"/);
  assert.match(toolbar, /class="pdf-tool-group" role="group" aria-label="编辑工具"/);
  assert.match(toolbar, /class="pdf-page-controls" role="group" aria-label="PDF 页面与缩放"/);
  assert.doesNotMatch(toolbar, /[↶↻‹›−＋]/);
});

test("renders imported PDFs through the shared document workspace", async () => {
  const vite = await createServer({ server: { middlewareMode: true, hmr: false } });
  try {
    const { default: PdfEditor } = await vite.ssrLoadModule("/src/PdfEditor.vue");
    const html = await renderToString(createSSRApp(PdfEditor, {
      file: { name: "sample.pdf", size: 2048 },
    }));

    assert.match(html, /class="document-workspace pdf-editor"/);
    assert.match(html, /class="document-workspace-file"[^>]*><strong title="sample\.pdf">sample\.pdf<\/strong><span>2 KB<\/span>/);
    assert.match(html, /class="document-workspace-canvas"/);
    assert.match(html, /class="icon-button document-workspace-close"[^>]*aria-label="关闭 PDF 编辑器"/);
    assert.match(html, /class="pdf-page-stage/);
    assert.match(html, /aria-label="PDF 页面预览"/);
    assert.match(html, /pdf-editor-toolbar[\s\S]*擦除文字[\s\S]*添加图片签名/);
  } finally {
    await vite.close();
  }
});

test("opens history directly in the editor for each document type", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const pdfEditor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");
  const openHistory = app.match(/async function openHistory\(item, event\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(openHistory, /if \(latest\.type === "pdf"\)[\s\S]*?await openPdfItem\(latest, event\?\.currentTarget\)/);
  assert.match(openHistory, /await openEditor\(\{ id: latest\.id, title: latest\.title \|\| "扫描文档", api: latest\.api \}\)/);
  assert.match(app, /<DocumentWorkspace class="image-document-workspace"/);
  assert.match(app, /state\.pdfWorkspaceOpen && state\.pdfFile[\s\S]*?<PdfEditor ref="pdfEditor" :file="state\.pdfFile" :active="true"/);
  assert.match(app, /function renderPdf\(item, file, returnFocus = null\) \{[\s\S]*?pdfEditorReturnFocus = returnFocus\?\.isConnected[\s\S]*?state\.pdfWorkspaceOpen = true/);
  assert.match(app, /@close="closePdfEditor"/);
  assert.match(styles, /\.pdf-editor-modal \{ display: flex; \}/);
  assert.match(styles, /\.editor-modal\.pdf-editor-modal \.document-workspace\.pdf-editor \{[^}]*height: 100%/);
  assert.match(pdfEditor, /<DocumentWorkspace class="pdf-editor"/);
  assert.match(pdfEditor, /document-workspace-close[\s\S]*aria-label="关闭 PDF 编辑器"/);
  assert.doesNotMatch(app, /state\.view === 'viewer'|document-viewer|openDocumentViewer/);
});

test("isolates the PDF modal and restores focus to its opener", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(app, /class="app-shell" :inert="[^\"]*state\.pdfWorkspaceOpen/);
  assert.match(app, /watch\(\(\) => state\.pdfWorkspaceOpen && !state\.busy[\s\S]*?focusElement\(pdfEditorModal\.value\)/);
  assert.match(app, /<div v-if="state\.pdfWorkspaceOpen && state\.pdfFile" ref="pdfEditorModal"[^>]*role="dialog" aria-modal="true"[^>]*tabindex="-1"/);
  assert.match(app, /function closePdfEditor\(\) \{[\s\S]*?pdfEditorReturnFocus[\s\S]*?focusElement\(target\)/);
  assert.match(app, /const target = returnFocus\?\.isConnected\s*\?\s*returnFocus\s*:\s*pdfImportButton\.value \|\| pdfToolNavButton\.value/);
  assert.match(app, /if \(!state\.pdfWorkspaceOpen\) \{\s*pdfEditorReturnFocus = returnFocus\?\.isConnected/);
  assert.match(app, /@click="openHistory\(item, \$event\)"/);
  assert.match(app, /@click="openPdfToolHistory\(item, \$event\)"/);
});

test("pauses hidden PDF previews while keeping page edits in memory", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const hiddenPath = editor.match(/if \(!props\.active\) \{([\s\S]*?)\n  \}\n\n  const file = props\.file;/)?.[1] || "";

  assert.match(app, /state\.pdfWorkspaceOpen && state\.pdfFile[\s\S]*?<PdfEditor ref="pdfEditor" :file="state\.pdfFile" :active="true"/);
  assert.doesNotMatch(app, /viewer-only|state\.view === 'viewer'/);
  assert.match(hiddenPath, /pendingRender\.cancel\(\)/);
  assert.match(hiddenPath, /pdfDocument\.value\.cleanup\(\)/);
  assert.match(hiddenPath, /releasePdfCanvas\(pageCanvas\.value\)/);
  assert.match(hiddenPath, /releasePdfCanvas\(overlayCanvas\.value\)/);
  assert.doesNotMatch(hiddenPath, /pageStates\.clear\(\)|discardEraseDraft\(/);
});

test("coalesces PDF page rerenders from resize notifications", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const cleanup = editor.match(/onBeforeUnmount\(\(\) => \{([\s\S]*?)\n\}\);/)?.[1] || "";

  assert.match(editor, /const resizeRenderScheduler = createFrameScheduler\(\(\) => \{\s*if \(props\.active\) void renderCurrentPage\(\);\s*\}\)/);
  assert.match(editor, /new ResizeObserver\(\(\) => \{\s*if \(pointerAction && overlayCanvas\.value\) \{\s*rebasePdfOverlayPointerAction\(pointerAction, overlayCanvas\.value\.getBoundingClientRect\(\), pageSize\);\s*\}\s*if \(props\.active\) resizeRenderScheduler\.schedule\(\);\s*\}\)/);
  assert.match(editor, /function schedulePreviewSync\(\) \{\s*resizeRenderScheduler\.cancel\(\)/);
  assert.match(cleanup, /resizeRenderScheduler\.cancel\(\)/);
});

test("reuses signature-pad bounds during pointer movement", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const resize = editor.match(/function resizeDrawCanvas\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerDown = editor.match(/function drawPointerDown\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerMove = editor.match(/function drawPointerMove\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(resize, /if \(drawPointer\) rebaseSignaturePointer\(drawPointer, rect\)/);
  assert.match(pointerDown, /drawPointer = \{ point, stroke, rect, clientX: event\.clientX, clientY: event\.clientY \}/);
  assert.match(pointerMove, /const rect = drawPointer\.rect/);
  assert.match(pointerMove, /drawPointer\.clientX = event\.clientX/);
  assert.doesNotMatch(pointerMove, /getBoundingClientRect/);
});

test("samples stored pen points and records the pointer-up endpoint", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const resize = editor.match(/function resizeDrawCanvas\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerMove = editor.match(/function drawPointerMove\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerUp = editor.match(/function endDrawPointer\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(resize, /rebaseSignaturePointer\(drawPointer, rect\)/);
  assert.match(pointerMove, /appendSignaturePoint\(drawPointer\.stroke, point, rect\.width, rect\.height, sampleDistance\)/);
  assert.match(pointerUp, /appendSignaturePoint\(drawPointer\.stroke, point, rect\.width, rect\.height, 0\)/);
});

test("samples PDF eraser movement and includes the pointer-up endpoint", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const pointerMove = editor.match(/function onOverlayPointerMove\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerUp = editor.match(/function onOverlayPointerUp\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(pointerMove, /appendErasePoint\(pointerAction\.stroke, point, point\.rect, sampleDistance\)/);
  assert.match(pointerMove, /if \(!changed\) return/);
  assert.match(pointerUp, /appendErasePoint\(pointerAction\.stroke, point, point\.rect, 0\)/);
  assert.match(pointerUp, /drawOverlayNow\(\)/);
});

test("reuses PDF overlay bounds during pointer movement and refreshes them on resize", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const pointerDown = editor.match(/function onOverlayPointerDown\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerMove = editor.match(/function onOverlayPointerMove\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const pointerUp = editor.match(/function onOverlayPointerUp\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const resizeObserver = editor.match(/resizeObserver = new ResizeObserver\(\(\) => \{([\s\S]*?)\n  \}\);/)?.[1] || "";

  assert.match(pointerDown, /pointerAction = \{ type: "erase", stroke, rect: point\.rect, clientX: event\.clientX, clientY: event\.clientY \}/);
  assert.match(pointerDown, /rotation: selected\.rotation \|\| 0,\s*rect: point\.rect,\s*clientX: event\.clientX,\s*clientY: event\.clientY/);
  assert.match(pointerDown, /type: "move",[\s\S]*?rect: point\.rect,[\s\S]*?clientX: event\.clientX,[\s\S]*?clientY: event\.clientY/);
  assert.match(pointerMove, /normalizedPoint\(event, pointerAction\.rect\)/);
  assert.match(pointerMove, /pointerAction\.clientX = event\.clientX;\s*pointerAction\.clientY = event\.clientY/);
  assert.doesNotMatch(pointerMove, /getBoundingClientRect/);
  assert.match(pointerUp, /normalizedPoint\(event, pointerAction\.rect\)/);
  assert.match(resizeObserver, /rebasePdfOverlayPointerAction\(pointerAction, overlayCanvas\.value\.getBoundingClientRect\(\), pageSize\)/);
  assert.match(editor, /resizeObserver\.observe\(overlayCanvas\.value\)/);
});

test("reuses committed eraser pixels and matches the rendered canvas pixel ratio", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const drawOverlay = editor.match(/async function drawOverlay\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const inactivePath = editor.match(/if \(!props\.active\) \{([\s\S]*?)\n  \}/)?.[1] || "";
  const cleanup = editor.match(/onBeforeUnmount\(\(\) => \{([\s\S]*?)\n\}\);/)?.[1] || "";

  assert.match(drawOverlay, /eraseLayerCache\.draw\(/);
  assert.match(drawOverlay, /const pixelRatio = canvas\.width \/ pageSize\.width/);
  assert.match(drawOverlay, /paint: \(layerContext, strokes\) => paintEraseStrokes/);
  assert.match(drawOverlay, /if \(eraseLayer\) \{\s*context\.save\(\);\s*context\.setTransform\(1, 0, 0, 1, 0, 0\);\s*context\.drawImage\(eraseLayer, 0, 0\)/);
  assert.match(drawOverlay, /if \(!eraseLayer\) paintEraseStrokes\(context, state\.strokes, currentPageEraseColor\)/);
  assert.match(inactivePath, /eraseLayerCache\.clear\(\)/);
  assert.match(cleanup, /eraseLayerCache\.clear\(\)/);
});

test("decodes signatures in bounded batches and keeps rendering after an image failure", () => {
  const editor = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const drawOverlay = editor.match(/async function drawOverlay\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const exportPage = editor.match(/async function renderEditedPage\(index, state\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const drawSignatures = editor.match(/async function drawPageSignatures\(context, canvas, state\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(drawOverlay, /for await \(const \{ start, images \} of loadImageBatches\(signatures,/);
  assert.match(drawOverlay, /if \(!image\) continue/);
  assert.match(drawOverlay, /if \(drawSequence !== overlayRenderSequence \|\| sequence !== renderSequence \|\| canvas !== overlayCanvas\.value\) return/);
  assert.match(exportPage, /await drawPageSignatures\(context, canvas, state\)/);
  assert.match(drawSignatures, /for await \(const \{ start, images \} of loadImageBatches\(\s*signatures,/);
  assert.match(drawSignatures, /if \(!image\) throw new Error\("无法渲染签名图片"\)/);
});
