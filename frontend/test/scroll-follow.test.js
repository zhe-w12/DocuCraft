import assert from "node:assert/strict";
import test from "node:test";
import { isNearScrollEnd, restoreScrollPosition, scrollAdjustmentForAnchor } from "../src/scroll-follow.js";

test("follows content while the reader is at or near the end", () => {
  assert.equal(isNearScrollEnd({ scrollHeight: 1200, clientHeight: 600, scrollTop: 530 }), true);
  assert.equal(isNearScrollEnd({ scrollHeight: 1200, clientHeight: 600, scrollTop: 512 }), true);
});

test("preserves the reader position after they scroll away from the end", () => {
  assert.equal(isNearScrollEnd({ scrollHeight: 1200, clientHeight: 600, scrollTop: 500 }), false);
  assert.equal(isNearScrollEnd({ scrollHeight: 1200, clientHeight: 600, scrollTop: 0 }), false);
});

test("treats a short or missing scrolling surface safely", () => {
  assert.equal(isNearScrollEnd({ scrollHeight: 500, clientHeight: 600, scrollTop: 0 }), true);
  assert.equal(isNearScrollEnd(null), false);
  assert.equal(isNearScrollEnd({ scrollHeight: 1200, clientHeight: 600, scrollTop: 500 }, -10), false);
});

test("adjusts scroll position by the visible anchor's movement", () => {
  assert.equal(scrollAdjustmentForAnchor(240, 920), 680);
  assert.equal(scrollAdjustmentForAnchor(240, 240), 0);
  assert.equal(scrollAdjustmentForAnchor(Number.NaN, 10), 0);
});

test("restores a saved position within the new content bounds or follows new content", () => {
  const scroller = { scrollHeight: 1800, clientHeight: 600, scrollTop: 0 };

  restoreScrollPosition(scroller, 420);
  assert.equal(scroller.scrollTop, 420);

  restoreScrollPosition(scroller, 1700);
  assert.equal(scroller.scrollTop, 1200);

  restoreScrollPosition(scroller, 420, true);
  assert.equal(scroller.scrollTop, 1200);
});
