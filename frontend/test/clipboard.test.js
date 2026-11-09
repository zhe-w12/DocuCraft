import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { copyTextToClipboard } from "../src/clipboard.js";
import { shareLinkWithFallback } from "../src/share-link.js";

function createLegacyEnvironment({ copied = true } = {}) {
  let document;
  const originalFocus = { isConnected: true, focused: false, focus() { this.focused = true; document.activeElement = this; } };
  const body = { child: null, appendChild(element) { this.child = element; element.isConnected = true; } };
  document = {
    activeElement: originalFocus,
    body,
    copiedText: "",
    createElement() {
      return {
        style: {},
        value: "",
        isConnected: false,
        setAttribute() {},
        focus() { document.activeElement = this; },
        select() { this.selected = true; },
        setSelectionRange(start, end) { this.selection = [start, end]; },
        remove() { this.isConnected = false; body.child = null; },
      };
    },
    execCommand(command) {
      assert.equal(command, "copy");
      assert.ok(body.child?.selected);
      assert.deepEqual(body.child.selection, [0, body.child.value.length]);
      document.copiedText = body.child.value;
      return copied;
    },
  };
  return { document, originalFocus };
}

test("uses the native clipboard when the browser grants access", async () => {
  const writes = [];
  await copyTextToClipboard("文档内容", {
    navigator: { clipboard: { async writeText(value) { writes.push(value); } } },
  });

  assert.deepEqual(writes, ["文档内容"]);
});

test("falls back to a selected textarea when Clipboard API is unavailable", async () => {
  const environment = createLegacyEnvironment();
  await copyTextToClipboard("标题\n\n正文", environment);

  assert.equal(environment.document.copiedText, "标题\n\n正文");
  assert.equal(environment.document.body.child, null, "temporary textarea should be removed");
  assert.equal(environment.document.activeElement, environment.originalFocus);
  assert.equal(environment.originalFocus.focused, true);
});

test("falls back when the native clipboard rejects the write", async () => {
  const environment = createLegacyEnvironment();
  environment.navigator = { clipboard: { async writeText() { throw new Error("permission denied"); } } };

  await copyTextToClipboard("回答内容", environment);
  assert.equal(environment.document.copiedText, "回答内容");
});

test("falls back when the browser blocks access to the clipboard property", async () => {
  const environment = createLegacyEnvironment();
  environment.navigator = Object.defineProperty({}, "clipboard", {
    get() { throw new DOMException("Clipboard access is blocked", "SecurityError"); },
  });

  await copyTextToClipboard("HTTP 页面里的文档", environment);

  assert.equal(environment.document.copiedText, "HTTP 页面里的文档");
});

test("reports failure if neither native nor legacy clipboard access works", async () => {
  const environment = createLegacyEnvironment({ copied: false });
  await assert.rejects(copyTextToClipboard("正文", environment), /浏览器拒绝复制操作/);
  assert.equal(environment.document.body.child, null, "temporary textarea should still be removed");
});

test("prefers native sharing and does not copy after a successful share", async () => {
  const events = [];
  const result = await shareLinkWithFallback({
    url: "https://example.test/doc.pdf",
    async share() { events.push("share"); },
    async copy() { events.push("copy"); },
    async manualCopy() { events.push("manual"); },
  });

  assert.equal(result, "shared");
  assert.deepEqual(events, ["share"]);
});

test("does not copy or open a fallback when the user cancels native sharing", async () => {
  const events = [];
  const result = await shareLinkWithFallback({
    url: "https://example.test/doc.pdf",
    async share() { throw new DOMException("cancelled", "AbortError"); },
    async copy() { events.push("copy"); },
    async manualCopy() { events.push("manual"); },
  });

  assert.equal(result, "cancelled");
  assert.deepEqual(events, []);
});

test("copies the link when native sharing fails and the clipboard is available", async () => {
  const copied = [];
  const result = await shareLinkWithFallback({
    url: "https://example.test/doc.pdf",
    async share() { throw new Error("share unavailable"); },
    async copy(url) { copied.push(url); },
    async manualCopy() { assert.fail("manual copy should not be needed"); },
  });

  assert.equal(result, "copied");
  assert.deepEqual(copied, ["https://example.test/doc.pdf"]);
});

test("opens a selected-text fallback when both native sharing and clipboard fail", async () => {
  const fallback = [];
  const result = await shareLinkWithFallback({
    url: "https://example.test/doc.pdf",
    async share() { throw new Error("share unavailable"); },
    async copy() { throw new Error("clipboard denied"); },
    async manualCopy(url) { fallback.push(url); },
  });

  assert.equal(result, "manual");
  assert.deepEqual(fallback, ["https://example.test/doc.pdf"]);
});

test("routes all visible copy actions through the shared clipboard helper", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const manualCopyDialog = readFileSync(new URL("../src/ManualCopyDialog.vue", import.meta.url), "utf8");

  assert.match(workspace, /async function copyDocument\(\)[\s\S]*?copyTextToClipboard\(text\)/);
  assert.match(workspace, /async function copyDocument\(\)[\s\S]*?catch \{\s*await openManualCopy\(text\);/);
  assert.match(workspace, /async function copyMessage\(content\)[\s\S]*?copyTextToClipboard\(content\)/);
  assert.match(workspace, /async function copyMessage\(content\)[\s\S]*?catch \{\s*await openManualCopy\(content\);/);
  assert.match(workspace, /import ManualCopyDialog from "\.\/ManualCopyDialog\.vue"/);
  assert.match(workspace, /<ManualCopyDialog ref="manualCopyDialog" \/>/);
  assert.match(workspace, /function openManualCopy\(text\)[\s\S]*?manualCopyDialog\.value\.open\(text\)/);
  assert.match(app, /async function shareDocument\([\s\S]*?shareLinkWithFallback\(\{/);
  assert.match(app, /async function shareDocument\([\s\S]*?copy: copyTextToClipboard,[\s\S]*?manualCopy: openManualCopy/);
  assert.match(app, /import ManualCopyDialog from "\.\/ManualCopyDialog\.vue"/);
  assert.match(app, /<ManualCopyDialog ref="shareCopyDialog" title="复制分享链接"/);
  assert.match(app, /function openManualCopy\(text\)[\s\S]*?shareCopyDialog\.value\.open\(text\)/);
  assert.match(manualCopyDialog, /useId\(\)/);
  assert.match(manualCopyDialog, /<dialog[^>]*:aria-labelledby="titleId"[\s\S]*?<textarea[^>]*readonly[^>]*:aria-label="label"[^>]*@focus="selectText"/);
  assert.match(manualCopyDialog, /async function open\(value\)[\s\S]*?selectText\(\);[\s\S]*?function selectText\(\)/);
  assert.doesNotMatch(`${workspace}\n${app}`, /navigator\.clipboard\.writeText/);
});
