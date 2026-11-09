import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { releasePdfCanvas, resolvePdfCanvasPixelRatio } from "../src/pdf-render-sizing.js";

test("releases PDF canvas backing buffers", () => {
  const canvas = { width: 2400, height: 3200 };

  releasePdfCanvas(canvas);
  releasePdfCanvas(null);

  assert.deepEqual(canvas, { width: 0, height: 0 });
});

test("clears old PDF canvases after destroying the previous document", () => {
  const source = readFileSync(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const start = source.indexOf("async function loadFile(file) {");
  const end = source.indexOf("\nasync function renderCurrentPage", start);
  const loadFile = source.slice(start, end);
  const destroyIndex = loadFile.indexOf("await pdfDocument.value.destroy()");
  const clearPageIndex = loadFile.indexOf("releasePdfCanvas(pageCanvas.value)");
  const clearOverlayIndex = loadFile.indexOf("releasePdfCanvas(overlayCanvas.value)");

  assert.ok(destroyIndex >= 0);
  assert.ok(clearPageIndex > destroyIndex);
  assert.ok(clearOverlayIndex > clearPageIndex);
});

test("keeps normal PDF previews crisp on high-density screens", () => {
  assert.equal(resolvePdfCanvasPixelRatio(600, 800, 2), 2);
  assert.equal(resolvePdfCanvasPixelRatio(900, 1200, 1.5), 1.5);
});

test("caps oversized preview canvases by edge and total pixel count", () => {
  for (const [width, height, devicePixelRatio] of [
    [5000, 6000, 2],
    [5000, 4800, 2],
    [10_000, 10_000, 3],
    [1200, 12_000, 2],
  ]) {
    const ratio = resolvePdfCanvasPixelRatio(width, height, devicePixelRatio);
    const bitmapWidth = Math.ceil(width * ratio);
    const bitmapHeight = Math.ceil(height * ratio);
    assert.ok(bitmapWidth <= 4096);
    assert.ok(bitmapHeight <= 4096);
    assert.ok(bitmapWidth * bitmapHeight <= 16_000_000);
  }
});

test("handles invalid page and device dimensions safely", () => {
  assert.equal(resolvePdfCanvasPixelRatio(0, 1000, 2), 1);
  assert.equal(resolvePdfCanvasPixelRatio(Number.NaN, 1000, 2), 1);
  assert.equal(resolvePdfCanvasPixelRatio(600, 800, Number.NaN), 1);
});
