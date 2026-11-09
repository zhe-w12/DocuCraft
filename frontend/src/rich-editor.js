function nodeText(node) {
  return String(node?.nodeValue || "").replace(/\u00a0/g, " ");
}

function mathSource(element) {
  return element.querySelector?.('annotation[encoding="application/x-tex"]')?.textContent?.trim() || "";
}

function serializeInline(node) {
  if (node.nodeType === 3) return nodeText(node);
  if (node.nodeType !== 1) return "";

  const tag = node.tagName.toLowerCase();
  const tex = mathSource(node);
  if (tex && (tag === "section" || tag === "eqn" || node.classList.contains("katex-display"))) {
    return `\n\n$$\n${tex}\n$$\n\n`;
  }
  if (tex && (tag === "eq" || node.classList.contains("katex"))) return `$${tex}$`;
  if (tag === "br") return "\n";
  if (tag === "img" || tag === "script" || tag === "style") return "";

  const content = [...node.childNodes].map(serializeInline).join("");
  if (tag === "strong" || tag === "b") return `**${content}**`;
  if (tag === "em" || tag === "i") return `*${content}*`;
  if (tag === "del" || tag === "s") return `~~${content}~~`;
  if (tag === "code") return `\`${content.replace(/`/g, "\\`")}\``;
  if (tag === "a") return `[${content}](${node.getAttribute("href") || ""})`;
  return content;
}

function serializeList(list, depth = 0) {
  const ordered = list.tagName.toLowerCase() === "ol";
  const baseIndex = Number.parseInt(list.getAttribute("start") || "1", 10);
  return [...list.children]
    .filter(item => item.tagName.toLowerCase() === "li")
    .map((item, index) => {
      const marker = ordered ? `${baseIndex + index}.` : "-";
      const indent = "  ".repeat(depth);
      const parts = [];
      const nested = [];

      for (const child of item.childNodes) {
        if (child.nodeType === 1 && ["ul", "ol"].includes(child.tagName.toLowerCase())) {
          nested.push(serializeList(child, depth + 1));
        } else {
          const text = child.nodeType === 1 && child.tagName.toLowerCase() === "p"
            ? [...child.childNodes].map(serializeInline).join("")
            : serializeInline(child);
          if (text.trim()) parts.push(text.trim());
        }
      }

      const body = parts.join(" ").replace(/\s*\n\s*/g, " ");
      const line = `${indent}${marker} ${body}`.trimEnd();
      return [line, ...nested].filter(Boolean).join("\n");
    })
    .join("\n");
}

function serializeBlock(node) {
  if (node.nodeType === 3) return nodeText(node).trim();
  if (node.nodeType !== 1) return "";

  const tag = node.tagName.toLowerCase();
  const tex = mathSource(node);
  if (tex && (tag === "section" || tag === "eqn" || node.classList.contains("katex-display"))) {
    return `$$\n${tex}\n$$`;
  }
  if (/^h[1-6]$/.test(tag)) return `${"#".repeat(Number(tag[1]))} ${[...node.childNodes].map(serializeInline).join("").trim()}`;
  if (tag === "p") return [...node.childNodes].map(serializeInline).join("").trim();
  if (tag === "ul" || tag === "ol") return serializeList(node);
  if (tag === "blockquote") {
    const quote = serializeBlocks(node).split("\n").map(line => line ? `> ${line}` : ">").join("\n");
    return quote;
  }
  if (tag === "pre") {
    const code = node.textContent.replace(/\s+$/, "");
    const longestFence = Math.max(0, ...[...code.matchAll(/`+/g)].map(match => match[0].length));
    const fence = "`".repeat(Math.max(3, longestFence + 1));
    return `${fence}\n${code}\n${fence}`;
  }
  if (tag === "hr") return "---";
  if (tag === "div") return serializeBlocks(node) || [...node.childNodes].map(serializeInline).join("").trim();
  if (tag === "table") {
    const rows = [...node.querySelectorAll("tr")].map(row => [...row.children].map(cell => cell.textContent.trim()));
    if (!rows.length) return "";
    const line = cells => `| ${cells.join(" | ")} |`;
    return [line(rows[0]), line(rows[0].map(() => "---")), ...rows.slice(1).map(line)].join("\n");
  }
  if (tag === "script" || tag === "style") return "";
  return [...node.childNodes].map(serializeInline).join("").trim();
}

function serializeBlocks(root) {
  return [...root.childNodes]
    .map(node => serializeBlock(node))
    .filter(Boolean)
    .join("\n\n");
}

export function serializeEditableDocument(root) {
  return root ? serializeBlocks(root).replace(/\n{3,}/g, "\n\n").trim() : "";
}
