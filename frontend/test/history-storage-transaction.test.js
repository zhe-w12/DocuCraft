import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { commitHistoryBeforeFileMutation } from "../src/history-storage-transaction.js";

test("persists the next history before deleting local files", async () => {
  const events = [];

  await commitHistoryBeforeFileMutation({
    persistNextHistory() { events.push("persist-next"); return true; },
    restorePreviousHistory() { events.push("restore-previous"); return true; },
    mutateFiles() { events.push("delete-files"); },
  });

  assert.deepEqual(events, ["persist-next", "delete-files"]);
});

test("keeps local files when the updated history cannot be persisted", async () => {
  const events = [];

  await assert.rejects(commitHistoryBeforeFileMutation({
    persistNextHistory() { events.push("persist-next"); return false; },
    restorePreviousHistory() { events.push("restore-previous"); return true; },
    mutateFiles() { events.push("delete-files"); },
  }), /浏览器无法保存历史记录/);

  assert.deepEqual(events, ["persist-next", "restore-previous"]);
});

test("restores the previous history if the IndexedDB mutation fails", async () => {
  const events = [];

  await assert.rejects(commitHistoryBeforeFileMutation({
    persistNextHistory() { events.push("persist-next"); return true; },
    restorePreviousHistory() { events.push("restore-previous"); return true; },
    mutateFiles() { events.push("delete-files"); throw new Error("IndexedDB 删除失败"); },
  }), /IndexedDB 删除失败/);

  assert.deepEqual(events, ["persist-next", "delete-files", "restore-previous"]);
});

test("makes history rollback failure explicit after a failed file mutation", async () => {
  await assert.rejects(commitHistoryBeforeFileMutation({
    persistNextHistory: () => true,
    restorePreviousHistory: () => false,
    mutateFiles: () => { throw new Error("IndexedDB 删除失败"); },
  }), /历史记录回滚失败/);
});

test("uses the persistence transaction for PDF history deletion and clearing", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const removeHistory = app.match(/async function removeHistory\(item\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const clearHistory = app.match(/async function clearHistory\(\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(removeHistory, /await commitHistoryBeforeFileMutation\(/);
  assert.match(removeHistory, /persistNextHistory:[\s\S]*?state\.history = nextHistory;[\s\S]*?return writeHistory\(\)/);
  assert.match(removeHistory, /mutateFiles:[\s\S]*?LocalDocuments\.remove\(item\.id\)/);
  assert.match(clearHistory, /await commitHistoryBeforeFileMutation\(/);
  assert.match(clearHistory, /persistNextHistory:[\s\S]*?state\.history = \[\];[\s\S]*?return writeHistory\(\)/);
  assert.match(clearHistory, /mutateFiles:[\s\S]*?LocalDocuments\.removeMany\(ids\)/);
});
