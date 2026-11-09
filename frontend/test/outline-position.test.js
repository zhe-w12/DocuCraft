import assert from "node:assert/strict";
import test from "node:test";
import { findLastItemAtOrBefore } from "../src/outline-position.js";

test("selects the last ordered heading that has crossed the scroll threshold", () => {
  const items = [0, 1, 2, 3, 4].map(index => ({ id: `heading-${index}`, top: index * 100 }));

  assert.equal(findLastItemAtOrBefore(items, 249, item => item.top), items[2]);
  assert.equal(findLastItemAtOrBefore(items, -1, item => item.top), items[0]);
  assert.equal(findLastItemAtOrBefore(items, 400, item => item.top), items[4]);
});

test("measures logarithmically many headings in a long document", () => {
  const items = Array.from({ length: 4096 }, (_, index) => ({ id: index, top: index * 20 }));
  let measurements = 0;

  const current = findLastItemAtOrBefore(items, 40_950, item => {
    measurements += 1;
    return item.top;
  });

  assert.equal(current, items[2047]);
  assert.ok(measurements <= 13, `expected at most 13 measurements, received ${measurements}`);
});

test("falls back to ordered scanning if a heading cannot be measured", () => {
  const items = [{ top: 0 }, { top: null }, { top: 80 }, { top: 150 }];

  assert.equal(findLastItemAtOrBefore(items, 100, item => item.top), items[2]);
  assert.equal(findLastItemAtOrBefore([], 100, () => 0), null);
});
