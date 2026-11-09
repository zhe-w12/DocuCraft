import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { resolveImageDocumentTitle } from "../src/image-document-title.js";

const opaqueName = "9dd5aacadfbd56cb6e70823722e96ad.jpg";

test("keeps meaningful imported image filenames", () => {
  assert.equal(
    resolveImageDocumentTitle("申请表扫描件.png", { paragraphs: ["OCR 标题"] }),
    "申请表扫描件",
  );
});

test("uses the OCR title for hash-named image files", () => {
  assert.equal(resolveImageDocumentTitle(opaqueName, {
    paragraphs: ["广东省家庭经济困难学生认定申请表", "学校："],
    paragraph_styles: ["title", "body"],
  }), "广东省家庭经济困难学生认定申请表");
});

test("falls back through text, table cells, and a readable generic title", () => {
  assert.equal(resolveImageDocumentTitle(opaqueName, {
    paragraphs: ["", "  扫描\n正文  "],
  }), "扫描 正文");
  assert.equal(resolveImageDocumentTitle(opaqueName, {
    paragraphs: [],
    tables: [[ ["", "表格标题"] ]],
  }), "表格标题");
  assert.equal(resolveImageDocumentTitle(opaqueName, null), "扫描文档");
});

test("limits suggested titles by Unicode characters", () => {
  const title = resolveImageDocumentTitle(opaqueName, {
    paragraphs: ["标题".repeat(80)],
  });

  assert.equal(Array.from(title).length, 64);
  assert.ok(title.endsWith("…"));
});

test("uses the resolved title for new imports and legacy image history", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const convertImages = app.match(/async function convertImages\(fileList\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const openEditor = app.match(/async function openEditor\(document = state\.current\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(app, /import \{ resolveImageDocumentTitle \} from "\.\/image-document-title\.js"/);
  assert.match(convertImages, /title: resolveImageDocumentTitle\(document\.title \|\| file\.name, document\)/);
  assert.match(openEditor, /resolveImageDocumentTitle\([\s\S]*?editableDocument\.title \|\| refreshed\.title,[\s\S]*?refreshed,/);
  assert.match(openEditor, /updateImageHistoryTitle\(editableDocument\.id, editableDocument\.api, title\)/);
});
