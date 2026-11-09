import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  clipboardContainsFiles,
  escapePastedPlainText,
  insertSanitizedClipboardContent,
  sanitizeEditableHtml,
} from "../src/editable-paste.js";

function text(value) {
  return { nodeType: 3, nodeValue: value };
}

function element(tagName, attributes = {}, children = []) {
  return {
    nodeType: 1,
    tagName: tagName.toUpperCase(),
    childNodes: children,
    getAttribute: name => attributes[name] ?? null,
  };
}

function parserFor(...children) {
  return class TestParser {
    parseFromString() {
      return { body: { childNodes: children } };
    }
  };
}

test("removes active HTML and event attributes while keeping formatted text", () => {
  const input = element("div", { onclick: "alert(1)", style: "background:url(javascript:alert(1))" }, [
    element("p", {}, [text("安全文本 "), element("strong", {}, [text("加粗")])]),
    element("img", { src: "x", onerror: "alert(1)" }, []),
    element("script", {}, [text("alert(1)")]),
    element("iframe", { src: "https://evil.invalid" }, [text("恶意框架")]),
  ]);

  assert.equal(
    sanitizeEditableHtml("ignored", parserFor(input)),
    "<div><p>安全文本 <strong>加粗</strong></p></div>",
  );
});

test("keeps safe links and strips javascript, data, and control-character URLs", () => {
  const input = element("p", {}, [
    element("a", { href: "https://example.com/a?x=1&y=2", onclick: "run()" }, [text("安全链接")]),
    text(" "),
    element("a", { href: "java\nscript:alert(1)" }, [text("坏链接")]),
    text(" "),
    element("a", { href: "data:text/html,unsafe" }, [text("数据链接")]),
  ]);

  assert.equal(
    sanitizeEditableHtml("ignored", parserFor(input)),
    '<p><a href="https://example.com/a?x=1&amp;y=2">安全链接</a> <a>坏链接</a> <a>数据链接</a></p>',
  );
});

test("preserves safe table structure and KaTeX source annotations", () => {
  const input = element("table", {}, [element("tbody", {}, [element("tr", {}, [
    element("td", { colspan: "2", rowspan: "9999", style: "color:red" }, [text("单元格")]),
    element("td", { onclick: "run()" }, [
      element("section", { class: "katex-display attacker" }, [
        element("span", { class: "katex other" }, [
          element("math", {}, [element("semantics", {}, [
            element("annotation", { encoding: "application/x-tex", onclick: "run()" }, [text("x^2")]),
          ])]),
        ]),
      ]),
    ]),
  ])])]);

  assert.equal(
    sanitizeEditableHtml("ignored", parserFor(input)),
    '<table><tbody><tr><td colspan="2">单元格</td><td><section class="katex-display"><span class="katex"><math><semantics><annotation encoding="application/x-tex">x^2</annotation></semantics></math></span></section></td></tr></tbody></table>',
  );
});

test("escapes the plain-text fallback and keeps newlines as line breaks", () => {
  assert.equal(
    escapePastedPlainText("<script>alert(1)</script>\n下一行"),
    "&lt;script&gt;alert(1)&lt;/script&gt;<br>下一行",
  );
});

test("uses the sanitizer on the live document editor paste path", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const pasteHandler = workspace.match(/function pasteDocumentEditor\(event\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(workspace, /import \{ clipboardContainsFiles, insertSanitizedClipboardContent \} from "\.\/editable-paste\.js"/);
  assert.match(workspace, /@paste="pasteDocumentEditor"/);
  assert.match(pasteHandler, /const html = clipboard\?\.getData\("text\/html"\) \|\| ""/);
  assert.match(pasteHandler, /const plainText = clipboard\?\.getData\("text\/plain"\) \|\| ""/);
  assert.match(pasteHandler, /if \(!html\.trim\(\) && !plainText\) \{[\s\S]*clipboardContainsFiles\(clipboard\)[\s\S]*event\.preventDefault\(\)/);
  assert.match(pasteHandler, /编辑器不支持直接粘贴图片或文件/);
  assert.match(pasteHandler, /剪贴板里没有可粘贴的文字/);
  assert.match(pasteHandler, /event\.preventDefault\(\)[\s\S]*insertSanitizedClipboardContent\([\s\S]*html,[\s\S]*plainText,[\s\S]*saveDocumentEditor\(\)/);
});

test("detects clipboard payloads that contain only files", () => {
  assert.equal(clipboardContainsFiles(null), false);
  assert.equal(clipboardContainsFiles({ types: ["text/plain"], items: [{ kind: "string" }] }), false);
  assert.equal(clipboardContainsFiles({ types: ["Files"], items: [] }), true);
  assert.equal(clipboardContainsFiles({ types: ["image/png"], items: [{ kind: "file", type: "image/png" }] }), true);
});

test("prefers native insertion with sanitized HTML", () => {
  const root = { innerHTML: "before", contains: node => node === root };
  const range = { commonAncestorContainer: root };
  const selection = { rangeCount: 1, getRangeAt: () => range };
  const calls = [];
  const documentRef = {
    execCommand: (...args) => { calls.push(args); return true; },
  };

  assert.equal(insertSanitizedClipboardContent(root, "ignored", "fallback", {
    documentRef,
    windowRef: { getSelection: () => selection },
    Parser: parserFor(element("p", { onclick: "run()" }, [text("safe")]), element("script", {}, [text("bad")])),
  }), true);
  assert.deepEqual(calls, [["insertHTML", false, "<p>safe</p>"]]);
});

test("falls back to a range insertion when the native command is unavailable", () => {
  const root = { innerHTML: "before", contains: node => node === root };
  const rangeCalls = [];
  const range = {
    commonAncestorContainer: root,
    deleteContents: () => rangeCalls.push("delete"),
    insertNode: fragment => rangeCalls.push(["insert", fragment]),
    setStartAfter: node => rangeCalls.push(["after", node]),
    collapse: toStart => rangeCalls.push(["collapse", toStart]),
  };
  const selection = {
    rangeCount: 1,
    getRangeAt: () => range,
    removeAllRanges: () => rangeCalls.push("clear-selection"),
    addRange: value => rangeCalls.push(["select", value]),
  };
  let insertedHtml = "";
  const lastChild = { nodeName: "P" };
  const documentRef = {
    execCommand: () => false,
    createElement: name => {
      assert.equal(name, "template");
      return {
        content: { lastChild },
        set innerHTML(value) { insertedHtml = value; },
      };
    },
  };

  assert.equal(insertSanitizedClipboardContent(root, "ignored", "fallback", {
    documentRef,
    windowRef: { getSelection: () => selection },
    Parser: parserFor(element("p", {}, [text("safe")]), element("script", {}, [text("bad")])),
  }), true);
  assert.equal(insertedHtml, "<p>safe</p>");
  assert.deepEqual(rangeCalls.slice(0, 4), ["delete", ["insert", { lastChild }], ["after", lastChild], ["collapse", true]]);
  assert.deepEqual(rangeCalls.slice(4), ["clear-selection", ["select", range]]);
});

test("moves an outside selection into the editor before inserting", () => {
  const outside = {};
  const root = { innerHTML: "before", contains: node => node === root, focusCalls: 0, focus() { this.focusCalls += 1; } };
  const rangeCalls = [];
  const range = {
    commonAncestorContainer: root,
    selectNodeContents: node => rangeCalls.push(["contents", node]),
    collapse: toStart => rangeCalls.push(["collapse", toStart]),
  };
  const selectionCalls = [];
  let currentRange = { commonAncestorContainer: outside };
  const selection = {
    get rangeCount() { return 1; },
    getRangeAt: () => currentRange,
    removeAllRanges: () => selectionCalls.push("clear"),
    addRange: nextRange => { currentRange = nextRange; selectionCalls.push(["add", nextRange]); },
  };
  const documentRef = {
    createRange: () => range,
    execCommand: () => true,
  };

  assert.equal(insertSanitizedClipboardContent(root, "ignored", "fallback", {
    documentRef,
    windowRef: { getSelection: () => selection },
    Parser: parserFor(element("b", {}, [text("safe")])),
  }), true);
  assert.equal(root.focusCalls, 1);
  assert.deepEqual(rangeCalls, [["contents", root], ["collapse", false]]);
  assert.deepEqual(selectionCalls, ["clear", ["add", range]]);
  assert.equal(currentRange, range);
});

test("inserts escaped plain-text-only clipboard data", () => {
  const root = { innerHTML: "before", contains: node => node === root };
  const range = { commonAncestorContainer: root };
  const selection = { rangeCount: 1, getRangeAt: () => range };
  let inserted = "";

  assert.equal(insertSanitizedClipboardContent(root, "", "<script>bad</script>\nnext", {
    documentRef: { execCommand: (_command, _ui, html) => { inserted = html; return true; } },
    windowRef: { getSelection: () => selection },
    Parser: parserFor(),
  }), true);
  assert.equal(inserted, "&lt;script&gt;bad&lt;/script&gt;<br>next");
});
