import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("gives assistant answers a readable font size and a wider message column", () => {
  const styles = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

  const messageWidthRules = [...styles.matchAll(/\.assistant-messages \{ width: ([^;]+);/g)];
  assert.equal(messageWidthRules.at(-1)?.[1], "min(1600px, 100%)", "the final message-list rule should give answers more room on wide screens");
  assert.match(styles, /\.assistant-workspace\.is-chat \.assistant-thread \{ width: min\(1600px, 100%\);/);
  assert.match(styles, /\.assistant-workspace\.is-chat \.assistant-compose-area \{ width: min\(1600px, calc\(100% - 36px\)\);/);
  assert.match(styles, /\.assistant-message:not\(\.is-user\) \.assistant-message-body \{ flex: 1 1 auto; max-width: min\(1600px, calc\(100% - 35px\)\); \}/);
  assert.match(styles, /\.assistant-message-content \{[^}]*border-radius: 12px;[^}]*line-height: 1\.75; font-size: 18px;/);
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*?\.assistant-message-content \{ padding: 11px 13px; font-size: 16px; line-height: 1\.7; \}/);
  assert.doesNotMatch(styles, /\.assistant-view \{ height: calc\(100vh - 178px\); min-height: 510px; \}/);
  assert.doesNotMatch(styles, /\.assistant-workspace \{ width: min\(920px, 100%\); height: 100%; min-height: 510px;/);
  assert.doesNotMatch(styles, /\.assistant-view \{ height: auto; min-height: calc\(100dvh - 190px\); \}/);
  assert.doesNotMatch(styles, /\.assistant-compose-area \{ padding: 5px 0 0; \}/);
  assert.match(styles, /\.assistant-view \{ height: 100%; min-height: 0; \}/);
  assert.match(styles, /\.assistant-compose-area \{ padding: 7px 9px 5px; \}/);
  assert.doesNotMatch(styles, /\.assistant-message-content \{[^}]*font-size: 13px;/);
  assert.doesNotMatch(styles, /\.assistant-message-content \{ padding: 11px 12px; font-size: 12px; \}/);
  assert.doesNotMatch(styles, /\.assistant-message\.is-user \.assistant-message-content \{ background: #f1f2f6; \}/);
});
