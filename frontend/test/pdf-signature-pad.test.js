import assert from "node:assert/strict";
import test from "node:test";
import { appendSignaturePoint, drawSignatureStrokes, rebaseSignaturePointer } from "../src/pdf-signature-pad.js";

test("samples transient signature points in rendered pixels and preserves the exact endpoint", () => {
  const stroke = { width: 4, points: [{ x: 0, y: 0 }] };
  const width = 600;
  const height = 800;

  assert.equal(appendSignaturePoint(stroke, { x: 0.0005, y: 0.0005 }, width, height), false);
  assert.equal(appendSignaturePoint(stroke, { x: 0.001, y: 0.001 }, width, height), true);
  assert.equal(appendSignaturePoint(stroke, { x: 0.0015, y: 0.001 }, width, height, 0), true);
  assert.deepEqual(stroke.points, [
    { x: 0, y: 0 },
    { x: 0.001, y: 0.001 },
    { x: 0.0015, y: 0.001 },
  ]);
});

test("does not add duplicate signature points when forcing a quick-tap endpoint", () => {
  const stroke = { width: 3, points: [{ x: 0.4, y: 0.6 }] };

  assert.equal(appendSignaturePoint(stroke, { x: 0.4, y: 0.6 }, 300, 200, 0), false);
  assert.equal(stroke.points.length, 1);
});

test("rebases an active pen to the resized signature pad at its last screen position", () => {
  const pointer = {
    point: { x: 0.5, y: 0.5 },
    clientX: 150,
    clientY: 60,
    rect: { left: 100, top: 10, width: 100, height: 100 },
    stroke: { width: 4, points: [{ x: 0.5, y: 0.5 }] },
  };
  const resizedRect = { left: 50, top: 20, width: 200, height: 80 };

  assert.deepEqual(rebaseSignaturePointer(pointer, resizedRect), { x: 0.5, y: 0.5 });
  assert.equal(pointer.rect, resizedRect);

  pointer.clientX = 170;
  assert.deepEqual(rebaseSignaturePointer(pointer, resizedRect), { x: 0.6, y: 0.5 });
  assert.deepEqual(pointer.stroke.points.at(-1), { x: 0.6, y: 0.5 });
});

function createContext() {
  const calls = [];
  return {
    calls,
    save() { calls.push(["save"]); },
    restore() { calls.push(["restore"]); },
    clearRect(...args) { calls.push(["clearRect", ...args]); },
    beginPath() { calls.push(["beginPath"]); },
    arc(...args) { calls.push(["arc", ...args]); },
    fill() { calls.push(["fill"]); },
    moveTo(...args) { calls.push(["moveTo", ...args]); },
    lineTo(...args) { calls.push(["lineTo", ...args]); },
    stroke() { calls.push(["stroke"]); },
  };
}

test("redraws normalized signature strokes at the current canvas dimensions", () => {
  const context = createContext();
  const strokes = [{ width: 4, points: [{ x: 0.1, y: 0.2 }, { x: 0.5, y: 0.75 }] }];

  drawSignatureStrokes(context, strokes, 400, 200);

  assert.deepEqual(context.calls.find(([name]) => name === "moveTo"), ["moveTo", 40, 40]);
  assert.deepEqual(context.calls.find(([name]) => name === "lineTo"), ["lineTo", 200, 150]);
  assert.deepEqual(context.calls.find(([name]) => name === "clearRect"), ["clearRect", 0, 0, 400, 200]);
});

test("renders a quick tap as a visible signature dot", () => {
  const context = createContext();

  drawSignatureStrokes(context, [{ width: 6, points: [{ x: 0.25, y: 0.6 }] }], 200, 100);

  assert.deepEqual(context.calls.find(([name]) => name === "arc"), ["arc", 50, 60, 3, 0, Math.PI * 2]);
  assert.ok(context.calls.some(([name]) => name === "fill"));
});
