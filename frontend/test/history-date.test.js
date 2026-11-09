import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createHistoryDateFormatter } from "../src/history-date.js";

test("reuses one date formatter across history cards", () => {
  let constructions = 0;
  const formatDate = createHistoryDateFormatter(() => {
    constructions += 1;
    return { format: date => date.toISOString().slice(0, 10) };
  });

  assert.equal(formatDate(0), "1970-01-01");
  assert.equal(formatDate(86_400_000), "1970-01-02");
  assert.equal(formatDate("86400000"), "1970-01-02");
  assert.equal(constructions, 1);
});

test("labels invalid timestamps as unknown instead of implying they are recent", () => {
  const formatDate = createHistoryDateFormatter(() => ({
    format(date) {
      if (Number.isNaN(date.valueOf())) throw new RangeError("Invalid time value");
      return "valid";
    },
  }));

  assert.equal(formatDate("invalid"), "时间未知");
  assert.equal(formatDate(undefined), "时间未知");
  assert.equal(formatDate(null), "时间未知");
  assert.equal(formatDate("  "), "时间未知");
});

test("uses the shared date formatter for history subtitles", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(app, /const formatDate = createHistoryDateFormatter\(\)/);
  assert.match(app, /return `\$\{type\} · \$\{formatDate\(item\.createdAt\)\}`/);
});
