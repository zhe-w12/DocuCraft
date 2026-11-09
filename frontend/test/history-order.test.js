import assert from "node:assert/strict";
import test from "node:test";
import { orderDocumentHistory } from "../src/history-order.js";

test("orders document history newest first and keeps equal timestamps stable", () => {
  const newest = { id: "newest", createdAt: 300 };
  const firstAtSameTime = { id: "same-a", createdAt: 200 };
  const secondAtSameTime = { id: "same-b", createdAt: "200" };
  const oldest = { id: "oldest", createdAt: "1970-01-01T00:00:00.100Z" };

  assert.deepEqual(
    orderDocumentHistory([oldest, firstAtSameTime, newest, secondAtSameTime]),
    [newest, firstAtSameTime, secondAtSameTime, oldest],
  );
});

test("puts records without a valid date last and handles non-array input", () => {
  const dated = { id: "dated", createdAt: 1 };
  const blank = { id: "blank", createdAt: " " };
  const missing = { id: "missing", createdAt: null };
  const undated = { id: "undated", createdAt: "not a date" };

  assert.deepEqual(orderDocumentHistory([blank, dated, missing, undated]), [dated, blank, missing, undated]);
  assert.deepEqual(orderDocumentHistory(null), []);
});

test("keeps unknown dates after valid pre-epoch and epoch records", () => {
  const beforeEpoch = { id: "before-epoch", createdAt: -1 };
  const epoch = { id: "epoch", createdAt: 0 };
  const unknown = { id: "unknown", createdAt: null };

  assert.deepEqual(orderDocumentHistory([unknown, beforeEpoch, epoch]), [epoch, beforeEpoch, unknown]);
});
