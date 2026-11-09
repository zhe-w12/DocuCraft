import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createHistoryTitleMatcher } from "../src/history-search.js";

test("caches normalized titles per history item and refreshes changed titles", () => {
  let normalizationCount = 0;
  const matches = createHistoryTitleMatcher(title => {
    normalizationCount += 1;
    return title.toLocaleLowerCase();
  });
  const item = { title: "扫描报告" };

  assert.equal(matches(item, ""), true);
  assert.equal(normalizationCount, 0);
  assert.equal(matches(item, "扫描"), true);
  assert.equal(matches(item, "报告"), true);
  assert.equal(normalizationCount, 1);

  item.title = "合同扫描";
  assert.equal(matches(item, "合同"), true);
  assert.equal(normalizationCount, 2);
});

test("uses cached title matching in the full history filter", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(app, /const matchesHistoryTitle = createHistoryTitleMatcher\(\)/);
  assert.match(app, /return matchesHistoryTitle\(item, query\)/);
});
