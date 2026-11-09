const ALLOWED_TAGS = new Set([
  "A", "ANNOTATION", "B", "BLOCKQUOTE", "BR", "CODE", "DEL", "DIV", "EM", "EQ", "EQN",
  "H1", "H2", "H3", "H4", "H5", "H6", "HR", "I", "LI", "MATH", "MFRAC", "MI",
  "MN", "MO", "MOVER", "MPHANTOM", "MROOT", "MROW", "MSQRT", "MSUB", "MSUP", "MTEXT",
  "MUNDER", "MUNDEROVER", "OL", "P", "PRE", "S", "SECTION", "SEMANTICS", "SPAN",
  "STRONG", "TABLE", "TBODY", "TFOOT", "TD", "TH", "THEAD", "TR", "UL",
]);

const DROP_CONTENT_TAGS = new Set([
  "AUDIO", "BASE", "BUTTON", "CANVAS", "EMBED", "FORM", "IFRAME", "IMG", "INPUT", "LINK",
  "META", "OBJECT", "SCRIPT", "SELECT", "STYLE", "SVG", "TEMPLATE", "TEXTAREA", "VIDEO",
]);

const CLASS_ALLOWLIST = new Map([
  ["SECTION", new Set(["katex-display"])],
  ["SPAN", new Set(["katex", "katex-display"])],
]);

const VOID_TAGS = new Set(["BR", "HR"]);

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeHref(value) {
  const href = String(value || "").trim();
  if (!href || /[\u0000-\u001f\u007f]/.test(href)) return "";
  try {
    const protocol = new URL(href, "https://iscan.invalid/").protocol;
    return ["http:", "https:", "mailto:"].includes(protocol) ? href : "";
  } catch {
    return "";
  }
}

function safeAttributes(element, tag) {
  const attributes = [];
  if (tag === "A") {
    const href = safeHref(element.getAttribute?.("href"));
    if (href) attributes.push(`href="${escapeHtml(href)}"`);
  }

  if (tag === "OL") {
    const start = String(element.getAttribute?.("start") || "");
    if (/^-?\d{1,6}$/.test(start)) attributes.push(`start="${start}"`);
  }

  if (tag === "TD" || tag === "TH") {
    for (const name of ["colspan", "rowspan"]) {
      const value = String(element.getAttribute?.(name) || "");
      if (/^[1-9]\d{0,2}$/.test(value) && Number(value) <= 100) {
        attributes.push(`${name}="${value}"`);
      }
    }
  }

  if (tag === "ANNOTATION" && element.getAttribute?.("encoding") === "application/x-tex") {
    attributes.push('encoding="application/x-tex"');
  }

  const allowedClasses = CLASS_ALLOWLIST.get(tag);
  if (allowedClasses) {
    const classes = String(element.getAttribute?.("class") || "")
      .split(/\s+/)
      .filter(className => allowedClasses.has(className));
    if (classes.length) attributes.push(`class="${classes.join(" ")}"`);
  }

  return attributes.length ? ` ${attributes.join(" ")}` : "";
}

function serializeSafeNode(node) {
  if (node?.nodeType === 3) return escapeHtml(node.nodeValue || "");
  if (node?.nodeType !== 1) return "";

  const tag = String(node.tagName || "").toUpperCase();
  if (DROP_CONTENT_TAGS.has(tag)) return "";
  const children = [...(node.childNodes || [])].map(serializeSafeNode).join("");
  if (!ALLOWED_TAGS.has(tag)) return children;
  if (VOID_TAGS.has(tag)) return `<${tag.toLowerCase()}>`;
  return `<${tag.toLowerCase()}${safeAttributes(node, tag)}>${children}</${tag.toLowerCase()}>`;
}

export function sanitizeEditableHtml(html, Parser = globalThis.DOMParser) {
  if (typeof Parser !== "function") return "";
  try {
    const parsed = new Parser().parseFromString(String(html || ""), "text/html");
    return [...(parsed?.body?.childNodes || [])].map(serializeSafeNode).join("");
  } catch {
    return "";
  }
}

export function escapePastedPlainText(text) {
  return escapeHtml(text).replace(/\r\n?/g, "\n").replace(/\n/g, "<br>");
}

export function clipboardContainsFiles(clipboard) {
  if (!clipboard) return false;
  if (Array.from(clipboard.types || []).includes("Files")) return true;
  return Array.from(clipboard.items || []).some(item => item?.kind === "file");
}

function rangeIsInside(root, range) {
  const node = range?.commonAncestorContainer;
  return Boolean(node && (node === root || root.contains?.(node)));
}

export function insertSanitizedClipboardContent(
  root,
  html,
  plainText,
  {
    documentRef = globalThis.document,
    windowRef = globalThis.window,
    Parser = globalThis.DOMParser,
  } = {},
) {
  if (!root || !documentRef || !windowRef) return false;
  const safeHtml = sanitizeEditableHtml(html, Parser);
  const insertionHtml = safeHtml || escapePastedPlainText(plainText || "");
  if (!insertionHtml) return false;

  const selection = windowRef.getSelection?.();
  let range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  if (!rangeIsInside(root, range)) {
    root.focus?.();
    if (typeof documentRef.createRange !== "function") return false;
    range = documentRef.createRange();
    range.selectNodeContents(root);
    range.collapse(false);
    selection?.removeAllRanges();
    selection?.addRange(range);
  }

  const previousHtml = root.innerHTML;
  try {
    const inserted = documentRef.execCommand?.("insertHTML", false, insertionHtml);
    if (inserted || root.innerHTML !== previousHtml) return true;
  } catch {
    // Fall back to a sanitized range insertion when the native command is unavailable.
  }

  const currentRange = selection?.rangeCount ? selection.getRangeAt(0) : null;
  if (rangeIsInside(root, currentRange)) range = currentRange;
  try {
    const template = documentRef.createElement("template");
    template.innerHTML = insertionHtml;
    const fragment = template.content;
    const lastNode = fragment.lastChild;
    range.deleteContents();
    range.insertNode(fragment);
    if (lastNode) {
      range.setStartAfter(lastNode);
      range.collapse(true);
      selection?.removeAllRanges();
      selection?.addRange(range);
    }
    return true;
  } catch {
    return false;
  }
}
