import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { HISTORY_PAGE_SIZE, nextHistoryPageLimit, sliceHistoryPage } from "../src/history-pagination.js";

test("renders the history list in bounded increments", () => {
  const records = Array.from({ length: 95 }, (_, index) => ({ id: index }));

  assert.equal(HISTORY_PAGE_SIZE, 40);
  assert.equal(sliceHistoryPage(records, HISTORY_PAGE_SIZE).length, 40);
  assert.equal(nextHistoryPageLimit(40, records.length), 80);
  assert.equal(nextHistoryPageLimit(80, records.length), records.length);
  assert.equal(sliceHistoryPage(records, records.length).length, records.length);
});

test("handles invalid pagination inputs without exposing the full history", () => {
  const records = [{ id: 1 }, { id: 2 }];

  assert.deepEqual(sliceHistoryPage(records, Number.NaN), []);
  assert.equal(nextHistoryPageLimit(1, 2, 0), 2);
  assert.deepEqual(sliceHistoryPage(null, 40), []);
});

test("uses paginated history and lazy thumbnails in the history screen", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const historyPage = app.match(/<section v-if="state\.view === 'history'"[\s\S]*?<\/section>/)?.[0] || "";

  assert.match(historyPage, /v-for="item in visibleHistory"/);
  assert.match(historyPage, /loading="lazy" decoding="async"/);
  assert.match(historyPage, /v-if="hasMoreHistory"[^>]*@click="loadMoreHistory"/);
  assert.match(app, /watch\(\(\) => \[state\.historySearch, state\.historyFilter\]/);
  assert.match(app, /orderedHistory = computed\(\(\) => orderDocumentHistory\(state\.history\)\)/);
  assert.match(app, /return orderedHistory\.value\.filter\(item =>/);
  assert.match(historyPage, /v-model="state\.historySearch"[^>]*type="search"/);
  assert.match(historyPage, /v-for="filter in \[\{ id: 'all', label: '全部' \}, \{ id: 'image', label: '图片识别' \}, \{ id: 'pdf', label: 'PDF' \}\]/);
});
