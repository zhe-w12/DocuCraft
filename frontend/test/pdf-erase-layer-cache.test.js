import assert from "node:assert/strict";
import test from "node:test";
import { createEraseLayerCache } from "../src/pdf-erase-layer-cache.js";

function createCanvasHarness() {
  const operations = [];
  const context = {
    clearRect: (...args) => operations.push(["clearRect", ...args]),
    save: () => operations.push(["save"]),
    restore: () => operations.push(["restore"]),
    setTransform: (...args) => operations.push(["setTransform", ...args]),
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => context,
  };
  return { canvas, operations };
}

function layerOptions(overrides = {}) {
  return {
    pageIndex: 0,
    strokes: [{
      width: 0.02,
      points: Array.from({ length: 512 }, (_, index) => ({ x: index / 512, y: 0.4 })),
    }],
    width: 1200,
    height: 1600,
    pageWidth: 600,
    pageHeight: 800,
    pixelRatio: 2,
    paint: () => {},
    ...overrides,
  };
}

test("reuses committed pixels until page strokes or dimensions change", () => {
  const harness = createCanvasHarness();
  let canvasCreations = 0;
  let paints = 0;
  let pointScans = 0;
  const cache = createEraseLayerCache(() => {
    canvasCreations += 1;
    return harness.canvas;
  });
  const paint = () => { paints += 1; };
  const strokes = layerOptions().strokes;
  strokes.reduce = function (callback, initialValue) {
    pointScans += 1;
    return Array.prototype.reduce.call(this, callback, initialValue);
  };
  const options = layerOptions({ paint, strokes });

  assert.equal(cache.draw(options), harness.canvas);
  assert.equal(cache.draw(options), harness.canvas);
  assert.equal(canvasCreations, 1);
  assert.equal(paints, 1);
  assert.equal(pointScans, 1);

  options.strokes.push({ width: 0.03, points: [{ x: 0.5, y: 0.5 }] });
  cache.draw(options);
  cache.draw({ ...options, pageIndex: 1 });
  cache.draw({ ...options, width: 1400 });
  assert.equal(paints, 4);
  assert.equal(pointScans, 4);
  assert.deepEqual(harness.operations.filter(([name]) => name === "setTransform").at(-1), ["setTransform", 2, 0, 0, 2, 0, 0]);
});

test("clears the backing canvas when a page has no committed eraser strokes", () => {
  const harness = createCanvasHarness();
  const cache = createEraseLayerCache(() => harness.canvas);
  cache.draw(layerOptions());

  assert.equal(cache.draw(layerOptions({ strokes: [] })), null);
  assert.equal(harness.canvas.width, 0);
  assert.equal(harness.canvas.height, 0);
});

test("rebuilds the cached layer after an explicit clear", () => {
  const harness = createCanvasHarness();
  let paints = 0;
  const cache = createEraseLayerCache(() => harness.canvas);
  const options = layerOptions({ paint: () => { paints += 1; } });

  cache.draw(options);
  cache.clear();
  cache.draw(options);
  assert.equal(paints, 2);
});

test("avoids allocating a full-page cache for short paths or very large canvases", () => {
  let canvasCreations = 0;
  let pointScans = 0;
  const cache = createEraseLayerCache(() => {
    canvasCreations += 1;
    return createCanvasHarness().canvas;
  });
  const shortStrokes = [{ width: 0.02, points: [{ x: 0.2, y: 0.4 }] }];
  shortStrokes.reduce = function (callback, initialValue) {
    pointScans += 1;
    return Array.prototype.reduce.call(this, callback, initialValue);
  };
  const shortPath = layerOptions({ strokes: shortStrokes });
  const largeStrokes = layerOptions().strokes;
  largeStrokes.reduce = function (callback, initialValue) {
    pointScans += 1;
    return Array.prototype.reduce.call(this, callback, initialValue);
  };
  const largeCanvas = layerOptions({ strokes: largeStrokes, width: 3000, height: 2000 });

  assert.equal(cache.draw(shortPath), null);
  assert.equal(cache.draw(shortPath), null);
  assert.equal(cache.draw(largeCanvas), null);
  assert.equal(cache.draw(largeCanvas), null);
  assert.equal(canvasCreations, 0);
  assert.equal(pointScans, 2);
});
