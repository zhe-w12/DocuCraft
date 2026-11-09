import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { hasSameDocumentOutline, outlineEditableDocument } from "../src/document-outline.js";

test("builds the document outline from headings already present in the editor", () => {
  const headings = [
    { id: "old-first", tagName: "H1", textContent: "  项目   方案  " },
    { id: "old-second", tagName: "H3", textContent: "" },
  ];
  const editor = {
    querySelectorAll(selector) {
      assert.equal(selector, "h1, h2, h3, h4, h5, h6");
      return headings;
    },
  };

  assert.deepEqual(outlineEditableDocument(editor), [
    { id: "iscan-document-heading-1", title: "项目 方案", level: 1 },
    { id: "iscan-document-heading-2", title: "未命名标题", level: 3 },
  ]);
  assert.equal(headings[0].id, "iscan-document-heading-1");
  assert.equal(headings[1].id, "iscan-document-heading-2");
});

test("detects outline changes without treating body edits as outline changes", () => {
  const outline = [{ id: "heading-1", title: "方案", level: 1 }];

  assert.equal(hasSameDocumentOutline(outline, [{ ...outline[0] }]), true);
  assert.equal(hasSameDocumentOutline(outline, [{ ...outline[0], title: "新方案" }]), false);
  assert.equal(hasSameDocumentOutline(outline, [...outline, { id: "heading-2", title: "结论", level: 2 }]), false);
});

test("uses the live editor outline instead of rendering Markdown on each edit", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");

  assert.match(workspace, /editorMode\.value === "edit" \? editableDocumentOutline\.value : renderedDocument\.value\.outline/);
  assert.match(workspace, /const outline = outlineEditableDocument\(documentContentEditor\.value\);\s*if \(!hasSameDocumentOutline\(editableDocumentOutline\.value, outline\)\) editableDocumentOutline\.value = outline/);
  assert.match(workspace, /<div v-else class="document-content-reading" v-html="renderedDocument\.html"><\/div>/);
});

test("coalesces outline measurement during document scrolling and cancels it on unmount", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");

  assert.match(workspace, /const activeOutlineFrame = createFrameScheduler\(updateActiveOutline\)/);
  assert.match(workspace, /function onDocumentScroll\(\) \{\s*if \(!props\.active\) return;\s*followDocumentScroll\.value = isNearScrollEnd\(documentPageScrollElement\.value\);\s*activeOutlineFrame\.schedule\(\);/);
  assert.match(workspace, /activeOutlineFrame\.cancel\(\)/);
});

test("uses ordered lookup instead of measuring every heading during scroll", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const updateOutline = workspace.match(/function updateActiveOutline\(\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(updateOutline, /findLastItemAtOrBefore\(outline, threshold, item =>/);
  assert.doesNotMatch(updateOutline, /for \(const item of outline\)/);
});
