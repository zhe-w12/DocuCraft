import assert from "node:assert/strict";
import test from "node:test";
import katex from "katex";
import MarkdownIt from "markdown-it";
import texmath from "markdown-it-texmath";
import { containsMathSource, normalizeDisplayMathDelimiters, stripDocumentChrome } from "../src/markdown-rendering.js";

const markdown = new MarkdownIt({ html: false, breaks: true }).use(texmath, {
  engine: katex,
  delimiters: ["dollars", "brackets"],
  katexOptions: { throwOnError: false },
});
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);

test("loads the math renderer only for paired math syntax, not ordinary currency values", () => {
  for (const source of ["价格是 $12.50", "成本 $10，税费 $2", "转义后的金额 \\$12"]) {
    assert.equal(containsMathSource(source), false, source);
  }
  for (const source of ["`$x^2$`", "```tex\n$x^2$\n```", "~~~md\n\\(x+1\\)\n~~~", "    $x^2$"]) {
    assert.equal(containsMathSource(source), false, source);
  }
  assert.equal(containsMathSource("行内公式 $x^2$"), true);
  assert.equal(containsMathSource("显示公式 $$x^2$$"), true);
});

test("detects inline formulas without rescanning long unmatched dollar sequences", { timeout: 5000 }, () => {
  assert.equal(containsMathSource("$x$"), true);
  assert.equal(containsMathSource("$x$ and \\(y+1\\)"), true);
  assert.equal(containsMathSource("\\$x$"), false);
  assert.equal(containsMathSource("5$x$"), false);
  assert.equal(containsMathSource("$x$5"), false);
  assert.equal(containsMathSource("$a ".repeat(100_000)), false);
});

test("detects bracketed formulas after normalizing standalone math brackets", () => {
  assert.equal(containsMathSource("行内公式 \\(x+1\\)"), true);
  assert.equal(containsMathSource("\\[x+1\\]"), true);
  assert.equal(containsMathSource(["[", "\\mathbb{R}", "]"].join("\n")), true);
  assert.equal(containsMathSource(["[", "普通说明文字", "]"].join("\n")), false);
});

test("shows image alt text without loading or linking to its remote source", () => {
  const html = markdown.render("流程图：![部署流程](https://example.invalid/diagram.png)");
  assert.equal(html, "<p>流程图：部署流程</p>\n");
  assert.doesNotMatch(html, /<img\b|<a\b|example\.invalid/);

  const formattedAlt = markdown.render("![流程 <图> & 注释](https://example.invalid/diagram.png)");
  assert.equal(formattedAlt, "<p>流程 &lt;图&gt; &amp; 注释</p>\n");

  const normalLink = markdown.render("[查看说明](https://example.invalid/guide)");
  assert.match(normalLink, /<a href="https:\/\/example\.invalid\/guide">查看说明<\/a>/);
});

test("keeps image examples in inline and fenced code intact", () => {
  const inline = markdown.render("`![图](https://example.invalid/diagram.png)`");
  const fenced = markdown.render("```md\n![图](https://example.invalid/diagram.png)\n```");
  assert.match(inline, /<code>!\[图\]\(https:\/\/example\.invalid\/diagram\.png\)<\/code>/);
  assert.match(fenced, /<code class="language-md">!\[图\]\(https:\/\/example\.invalid\/diagram\.png\)/);
});

test("masks inline code spans without backtracking across unmatched tick runs", { timeout: 5000 }, () => {
  const unmatchedRuns = Array.from({ length: 1200 }, (_, index) => (
    `x${"`".repeat(1200 - index)}z`
  )).join("\n");

  assert.equal(containsMathSource(`${unmatchedRuns}\n$x$`), true);
});

test("escapes raw HTML and rejects unsafe link protocols in model output", () => {
  const html = markdown.render("<img src=x onerror=alert(1)>\n\n[打开](javascript:alert(1))");
  assert.doesNotMatch(html, /<img\b|\shref=[\"'](?:javascript|data|vbscript):/i);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("normalizes multiline display math and separates equations around blank lines", () => {
  const source = [
    "若条件满足：",
    "$$",
    "**\\lim_{x\\to a}\\frac{f(x)}{g(x)}**",
    "",
    "\\lim_{x\\to a}\\frac{f'(x)}{g'(x)}",
    "$$",
  ].join("\n");

  const normalized = normalizeDisplayMathDelimiters(source);
  const html = markdown.render(normalized);

  assert.match(normalized, /\$\$\\lim_\{x\\to a\}\\frac\{f\(x\)\}\{g\(x\)\}\$\$/);
  assert.equal((html.match(/class="katex-display"/g) || []).length, 2);
  assert.doesNotMatch(html, /若条件满足：[\s\S]*?\$\$/);
  assert.doesNotMatch(html.replace(/<annotation[\s\S]*?<\/annotation>/g, ""), /\\lim_/);
});

test("does not rewrite examples inside fenced code blocks", () => {
  const source = "```tex\n$$\nx^2\n$$\n```";
  assert.equal(normalizeDisplayMathDelimiters(source), source);
});

test("normalizes multiline bracket display math into a rendered equation", () => {
  const source = "\\[\n\\frac{1}{2}\n\\]";
  const html = markdown.render(normalizeDisplayMathDelimiters(source));
  assert.match(html, /class="katex-display"/);
  assert.doesNotMatch(html.replace(/<annotation[\s\S]*?<\/annotation>/g, ""), /\\\[|\\frac/);
});

test("renders bare bracket-wrapped TeX blocks returned by the model", () => {
  const source = [
    "常见极限：",
    "[",
    "\\lim_{x\\to 0}\\frac{\\sin x}{x}=1",
    "]",
    "",
    "[",
    "\\mathbb{R}",
    "]",
  ].join("\n");

  const normalized = normalizeDisplayMathDelimiters(source);
  const html = markdown.render(normalized);

  assert.match(normalized, /\$\$\\lim_\{x\\to 0\}\\frac\{\\sin x\}\{x\}=1\$\$/);
  assert.match(normalized, /\$\$\\mathbb\{R\}\$\$/);
  assert.equal((html.match(/class="katex-display"/g) || []).length, 2);
  assert.doesNotMatch(html.replace(/<annotation[\s\S]*?<\/annotation>/g, ""), /\\lim_|\[|\]/);
});

test("leaves ordinary bracket text and bracket examples in code fences unchanged", () => {
  const source = [
    "选项：[正确]",
    "```text",
    "[",
    "\\lim_{x\\to 0} x",
    "]",
    "```",
  ].join("\n");

  assert.equal(normalizeDisplayMathDelimiters(source), source);
});

test("handles many unmatched standalone opening brackets without repeated rescans", { timeout: 5000 }, () => {
  const source = Array(40_000).fill("[").join("\n");

  assert.equal(normalizeDisplayMathDelimiters(source), source);
});

test("handles many unmatched standalone TeX brackets without repeated rescans", { timeout: 5000 }, () => {
  const source = Array(40_000).fill("\\[").join("\n");

  assert.equal(normalizeDisplayMathDelimiters(source), source);
});

test("preserves ordinary bracket blocks and still normalizes a later formula", () => {
  const source = ["[", "普通说明文字", "]", "[", "\\mathbb{R}", "]"].join("\n");

  assert.equal(normalizeDisplayMathDelimiters(source), "[\n普通说明文字\n]\n$$\\mathbb{R}$$");
});

test("does not pair a display-math opener with a closer inside a code fence", () => {
  const source = ["\\[", "```tex", "\\]", "```", "正文", "\\]"].join("\n");

  assert.equal(normalizeDisplayMathDelimiters(source), source);
});

test("removes faux workspace and autosave metadata from document content", () => {
  const source = "正文开头。\n\n👤 你　我的工作区 | 自动保存\n\n## 第一节\n内容。";
  assert.equal(stripDocumentChrome(source), "正文开头。\n\n## 第一节\n内容。");
});

test("removes split and markdown-formatted workspace metadata, including its author badge", () => {
  const variants = [
    "开头\n\n👤 你\n我的工作区 | 自动保存\n\n正文",
    "开头\n\n| 你 | 我的工作区 | 自动保存 |\n\n正文",
    "开头\n\n> 你\n> 我的工作区 · 自动保存\n\n正文",
    "开头\n\n<span>你</span>\n<span>我的工作区｜自动保存</span>\n\n正文",
    "开头\n\n![你](avatar.png)&nbsp;我的工作区 | 自动保存到当前对话\n\n正文",
    "开头\n\n你\n我的工作区 | 保存到当前对话\n\n正文",
    "开头\n\n你\n\n我的工作区\n\n自动保存\n\n正文",
    "开头\n\n我的工作区\n自动保存\n\n正文",
    "开头\n\n我的工作区\n\n保存到当前对话\n\n正文",
  ];

  for (const source of variants) assert.equal(stripDocumentChrome(source), "开头\n\n正文");
  assert.equal(stripDocumentChrome("正文\n\n你\n\n下一段"), "正文\n\n你\n\n下一段");
});

test("removes decorated and multi-line workspace metadata rows", () => {
  const source = [
    "前言",
    "",
    "![头像](avatar.png)",
    "",
    "👤 你",
    "**我的工作区** ｜ 自动保存",
    "",
    "## 第一节",
    "正文",
  ].join("\n");

  assert.equal(stripDocumentChrome(source), "前言\n\n## 第一节\n正文");
  assert.equal(
    stripDocumentChrome("标题\n\n![头像](avatar.png) 你 | 我的工作区 | 自动保存\n\n正文"),
    "标题\n\n正文",
  );
});

test("keeps prose that mentions workspace and autosave outside a metadata row", () => {
  const source = "请在我的工作区检查自动保存状态是否开启。";
  assert.equal(stripDocumentChrome(source), source);
});
