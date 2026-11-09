import assert from "node:assert/strict";
import test from "node:test";
import { serializeEditableDocument } from "../src/rich-editor.js";

function text(value) {
  return { nodeType: 3, nodeValue: value };
}

function element(tagName, children = [], attributes = {}, classes = []) {
  const node = {
    nodeType: 1,
    tagName: tagName.toUpperCase(),
    childNodes: children,
    children: children.filter(child => child.nodeType === 1),
    classList: { contains: className => classes.includes(className) },
    getAttribute: name => attributes[name] ?? null,
    querySelector(selector) {
      const expectedTag = selector.slice(0, selector.indexOf("[")).toLowerCase();
      const visit = current => {
        for (const child of current.childNodes || []) {
          if (child.nodeType !== 1) continue;
          if (child.tagName.toLowerCase() === expectedTag && child.getAttribute("encoding") === "application/x-tex") return child;
          const nested = visit(child);
          if (nested) return nested;
        }
        return null;
      };
      return visit(node);
    },
  };
  Object.defineProperty(node, "textContent", {
    get: () => children.map(child => child.nodeType === 3 ? child.nodeValue : child.textContent).join(""),
  });
  return node;
}

test("serializes rendered formatting and KaTeX back to editable Markdown", () => {
  const katex = element("span", [
    element("span", [
      element("annotation", [text("x^2 + 1")], { encoding: "application/x-tex" }),
    ]),
  ], {}, ["katex"]);
  const formula = element("section", [element("eqn", [element("span", [katex], {}, ["katex-display"])])]);
  const root = element("div", [
    element("h2", [text("函数基础")]),
    element("p", [text("保留 "), element("strong", [text("重点")]), text("。")]),
    element("ul", [
      element("li", [element("p", [text("函数定义")])]),
      element("li", [text("常见函数")]),
    ]),
    formula,
  ]);

  assert.equal(serializeEditableDocument(root), [
    "## 函数基础",
    "保留 **重点**。",
    "- 函数定义\n- 常见函数",
    "$$\nx^2 + 1\n$$",
  ].join("\n\n"));
});
