import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

function extractBlock(source, openingBrace) {
  let depth = 0;
  for (let index = openingBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(openingBrace + 1, index);
  }
  return "";
}

function narrowRulesFrom(source, viewportWidth) {
  const rules = [];
  const mediaPattern = /@media\s*\(([^)]*)\)\s*\{/g;
  for (const media of source.matchAll(mediaPattern)) {
    const maxWidth = /max-width:\s*([\d.]+)px/.exec(media[1]);
    if (!maxWidth || viewportWidth > Number(maxWidth[1])) continue;
    const body = extractBlock(source, media.index + media[0].lastIndexOf("{"));
    rules.push(...[...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
      selector: match[1].trim(),
      declarations: match[2],
    })));
  }
  return rules;
}

function targetHeight(rules, selector) {
  let minHeight = 0;
  let height = 0;
  for (const rule of rules) {
    if (!rule.selector.split(",").map((item) => item.trim()).includes(selector)) continue;
    for (const declaration of rule.declarations.matchAll(/(?:^|;)\s*(min-height|height)\s*:\s*([\d.]+)px\b/g)) {
      if (declaration[1] === "min-height") minHeight = Number.parseFloat(declaration[2]);
      else height = Number.parseFloat(declaration[2]);
    }
  }
  return Math.max(minHeight, height);
}

function targetWidth(rules, selector) {
  let minWidth = 0;
  let width = 0;
  for (const rule of rules) {
    if (!rule.selector.split(",").map((item) => item.trim()).includes(selector)) continue;
    for (const declaration of rule.declarations.matchAll(/(?:^|;)\s*(min-width|width)\s*:\s*([^;]+)/g)) {
      if (declaration[1] === "min-width") minWidth = Number.parseFloat(declaration[2]) || 0;
      else width = declaration[2].trim() === "auto" ? 0 : Number.parseFloat(declaration[2]) || 0;
    }
  }
  return Math.max(minWidth, width);
}

test("narrow PDF, document and assistant controls meet their touch-size targets", async () => {
  const globalCss = await readFile(new URL("../styles.css", import.meta.url), "utf8");
  const pdfSource = await readFile(new URL("../src/PdfEditor.vue", import.meta.url), "utf8");
  const pdfStyle = pdfSource.match(/<style scoped>([\s\S]*?)<\/style>/)?.[1];
  assert.ok(pdfStyle, "PDF editor styles should be available for touch-target checks");
  const targetSelectors = [
    ".assistant-setup-note button",
    ".assistant-other-request button",
    ".assistant-load-older",
    ".scroll-to-latest",
    ".assistant-panel-tool",
    ".assistant-document-open-button",
    ".assistant-message-actions button",
    ".assistant-home-composer .assistant-send-button",
    ".assistant-compose-area .assistant-send-button",
    ".tool-page-header .primary-button",
    ".assistant-mobile-tabs button",
    ".document-tool-button",
    ".document-outline-document-title",
    ".document-outline-item",
    ".image-editor-actions .image-editor-download",
  ];
  const pdfSelectors = [".pdf-tool-button", ".pdf-editor-actions .ghost-button", ".pdf-editor-actions .primary-button"];

  for (const viewportWidth of [760, 700, 620, 375, 320]) {
    const globalRules = narrowRulesFrom(globalCss, viewportWidth);
    const pdfRules = narrowRulesFrom(pdfStyle, viewportWidth);
    assert.ok(targetWidth(globalRules, ".nav-item") >= 44, `navigation items should be at least 44px wide at ${viewportWidth}px`);
    for (const selector of targetSelectors) {
      assert.ok(targetHeight(globalRules, selector) >= 44, `${selector} should be at least 44px high at ${viewportWidth}px`);
    }
    assert.ok(targetHeight(globalRules, ".assistant-mode-select select") >= 40, `workflow selector should be at least 40px high at ${viewportWidth}px`);
    for (const selector of pdfSelectors) {
      assert.ok(targetHeight(pdfRules, selector) >= 44, `${selector} should be at least 44px high at ${viewportWidth}px`);
    }
  }
});
