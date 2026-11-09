import assert from "node:assert/strict";
import test from "node:test";
import { appendErasePoint, commitEraseDraft, discardEraseDraft, stageEraseStroke } from "../src/pdf-erase-draft.js";

test("samples erase points in rendered pixels while preserving short movements and the final endpoint", () => {
  const stroke = { width: 0.024, points: [{ x: 0, y: 0 }] };
  const rect = { width: 1000, height: 500 };

  assert.equal(appendErasePoint(stroke, { x: 0.001, y: 0.003 }, rect, 2), false);
  assert.equal(appendErasePoint(stroke, { x: 0.002, y: 0.004 }, rect, 2), true);
  assert.equal(appendErasePoint(stroke, { x: 0.0025, y: 0.004 }, rect, 0), true);
  assert.deepEqual(stroke.points, [
    { x: 0, y: 0 },
    { x: 0.002, y: 0.004 },
    { x: 0.0025, y: 0.004 },
  ]);
});

test("does not store repeated pointer coordinates even when endpoint sampling is forced", () => {
  const stroke = { width: 0.02, points: [{ x: 0.4, y: 0.6 }] };

  assert.equal(appendErasePoint(stroke, { x: 0.4, y: 0.6 }, { width: 600, height: 800 }, 0), false);
  assert.deepEqual(stroke.points, [{ x: 0.4, y: 0.6 }]);
});

test("staged erase strokes stay a preview until the user confirms them", () => {
  const page = { strokes: [] };
  const draft = [];
  const stroke = { width: 0.04, points: [{ x: 0.2, y: 0.3 }] };

  stageEraseStroke(draft, stroke);
  assert.deepEqual(page.strokes, []);
  assert.deepEqual(draft, [stroke]);

  assert.equal(commitEraseDraft(draft, page), true);
  assert.deepEqual(page.strokes, [stroke]);
  assert.deepEqual(draft, []);
});

test("cancelling the erase preview leaves confirmed page annotations untouched", () => {
  const committed = { width: 0.02, points: [{ x: 0.1, y: 0.1 }] };
  const page = { strokes: [committed] };
  const draft = [{ width: 0.04, points: [{ x: 0.7, y: 0.7 }] }];

  discardEraseDraft(draft);
  assert.deepEqual(page.strokes, [committed]);
  assert.deepEqual(draft, []);
  assert.equal(commitEraseDraft(draft, page), false);
});
