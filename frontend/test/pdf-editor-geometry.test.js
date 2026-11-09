import test from "node:test";
import assert from "node:assert/strict";
import { clamp, inverseRotatePointAroundCenter, normalizePoint, rotatedBounds, rotatePointAroundCenter, toCanvasPoint } from "../src/pdf-editor-geometry.js";

test("normalizes pointer coordinates against the rendered page and clamps outside drags", () => {
  const rect = { left: 10, top: 20, width: 600, height: 800 };
  assert.deepEqual(normalizePoint(160, 180, rect), { x: 0.25, y: 0.2 });
  assert.deepEqual(normalizePoint(900, -10, rect), { x: 1, y: 0 });
});

test("converts normalized strokes and signatures to rendered canvas pixels", () => {
  assert.deepEqual(toCanvasPoint({ x: 0.1, y: 0.2 }, 600, 800), { x: 60, y: 160 });
});

test("clamps zoom, brush and signature coordinates to their legal ranges", () => {
  assert.equal(clamp(-1, 0, 1), 0);
  assert.equal(clamp(0.5, 0, 1), 0.5);
  assert.equal(clamp(3, 0, 1), 1);
});

test("rotates points around the signature center and maps hit tests back to local coordinates", () => {
  const rotated = rotatePointAroundCenter(15, 20, 10, 20, 90);
  assert.ok(Math.abs(rotated.x - 10) < 1e-10);
  assert.ok(Math.abs(rotated.y - 25) < 1e-10);
  const restored = inverseRotatePointAroundCenter(rotated.x, rotated.y, 10, 20, 90);
  assert.ok(Math.abs(restored.x - 15) < 1e-10);
  assert.ok(Math.abs(restored.y - 20) < 1e-10);
});

test("computes the full page footprint of a rotated signature", () => {
  const quarterTurn = rotatedBounds(100, 40, 90);
  assert.ok(Math.abs(quarterTurn.width - 40) < 1e-10);
  assert.ok(Math.abs(quarterTurn.height - 100) < 1e-10);
  const diagonal = rotatedBounds(100, 100, 45);
  assert.ok(Math.abs(diagonal.width - Math.SQRT2 * 100) < 1e-10);
  assert.ok(Math.abs(diagonal.height - Math.SQRT2 * 100) < 1e-10);
});
