import assert from "node:assert/strict";
import test from "node:test";
import { prepareEditorWorkspaceHtml, prepareImageEditorWorkspaceHtml } from "../src/editor-workspace-html.js";

test("expands the OCR editor toolbar into the shared document workspace", () => {
  const source = "<html><head><style>.toolbar{width:min(794px,100%)}.page{margin:0 auto}</style></head><body></body></html>";
  const result = prepareEditorWorkspaceHtml(source);

  assert.match(result, /\.toolbar\{width:min\(794px,100%\)\}/);
  assert.match(result, /\.toolbar\{width:100%;max-width:none/);
  assert.match(result, /\.page,\.original-page,\.coordinate-page\{margin-top:22px/);
});

test("leaves malformed editor HTML unchanged", () => {
  assert.equal(prepareEditorWorkspaceHtml("<html><body>document</body></html>"), "<html><body>document</body></html>");
});

test("keeps editable OCR documents on the original source layout", () => {
  const source = "<html><head><style>.toolbar{width:min(794px,100%)}</style></head><body>"
    + "<button class='view-button' id='coordinateToggle' onclick=toggleCoordinate()>结构编辑</button>"
    + "<div id='coordinatePage'><span data-coordinate-cell='1' contenteditable='true'>姓名</span></div>"
    + "<div class='page' id='page' style='display:none'>reflowed table</div></body></html>";
  const result = prepareImageEditorWorkspaceHtml(source);

  assert.match(result, /<button class='view-button is-current' type='button' disabled aria-current='page'>版式编辑<\/button>/);
  assert.doesNotMatch(result, /id='coordinateToggle'|onclick=toggleCoordinate\(\)/);
  assert.match(result, /<div id='coordinatePage'>/);
  assert.match(result, /style='display:none'>reflowed table/);
  assert.match(result, /\.toolbar button\.view-button\.is-current:disabled\{opacity:1;cursor:default/);
});

test("keeps the structure switch when source-positioned editable fields are unavailable", () => {
  const source = "<html><head><style></style></head><body>"
    + "<button class='view-button' id='coordinateToggle' onclick=toggleCoordinate()>结构编辑</button>"
    + "<div id='coordinatePage'><img alt='source'></div></body></html>";

  assert.match(prepareImageEditorWorkspaceHtml(source), /id='coordinateToggle'/);
});

test("locks older double-quoted source-layout controls to the editable canvas too", () => {
  const source = '<html><head><style></style></head><body>'
    + '<button title="切换版式" id="coordinateToggle" onclick="toggleCoordinate()">结构编辑</button>'
    + '<div id="coordinatePage"><span data-positioned-word="1" contenteditable="true">标题</span></div>'
    + '</body></html>';

  assert.match(prepareImageEditorWorkspaceHtml(source), /aria-current='page'>版式编辑<\/button>/);
  assert.doesNotMatch(prepareImageEditorWorkspaceHtml(source), /id="coordinateToggle"/);
});
