import assert from "node:assert/strict";
import test from "node:test";
import { rebasePdfOverlayPointerAction } from "../src/pdf-overlay-pointer.js";

test("rebases signature movement to the current pointer position after a canvas resize", () => {
  const item = { x: 0.3, y: 0.4 };
  const action = {
    type: "move",
    item,
    x: 0.2,
    y: 0.25,
    startX: 0.5,
    startY: 0.5,
    clientX: 160,
    clientY: 80,
  };
  const rect = { left: 60, top: 20, width: 200, height: 120 };

  assert.deepEqual(rebasePdfOverlayPointerAction(action, rect, { width: 200, height: 120 }), { x: 0.5, y: 0.5 });
  assert.equal(action.rect, rect);
  assert.deepEqual([action.startX, action.startY, action.x, action.y], [0.5, 0.5, 0.3, 0.4]);
});

test("rebases signature resizing from its current size to avoid a resize jump", () => {
  const item = { x: 0.3, y: 0.4, width: 0.2, height: 0.1 };
  const action = {
    type: "resize",
    item,
    startX: 100,
    startY: 80,
    startWidth: 0.15,
    startHeight: 0.075,
    clientX: 160,
    clientY: 80,
  };
  const rect = { left: 60, top: 20, width: 200, height: 120 };

  assert.deepEqual(rebasePdfOverlayPointerAction(action, rect, { width: 400, height: 240 }), { x: 0.5, y: 0.5 });
  assert.deepEqual([action.startX, action.startY, action.startWidth, action.startHeight], [200, 120, 0.2, 0.1]);
});

test("updates cached bounds for eraser gestures without changing their action", () => {
  const action = { type: "erase", stroke: { points: [] }, clientX: 160, clientY: 80 };
  const rect = { left: 60, top: 20, width: 200, height: 120 };

  rebasePdfOverlayPointerAction(action, rect, { width: 200, height: 120 });

  assert.equal(action.rect, rect);
  assert.equal(action.type, "erase");
  assert.deepEqual(action.stroke.points, []);
});
