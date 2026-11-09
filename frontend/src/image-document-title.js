const OPAQUE_FILE_TITLE = /^[a-f\d]{24,64}$/i;
const MAX_TITLE_LENGTH = 64;

function normalizeCandidate(value) {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

function truncateTitle(value) {
  const characters = Array.from(value);
  if (characters.length <= MAX_TITLE_LENGTH) return value;
  return `${characters.slice(0, MAX_TITLE_LENGTH - 1).join("")}…`;
}

function firstTableText(tables) {
  for (const table of Array.isArray(tables) ? tables : []) {
    for (const row of Array.isArray(table) ? table : []) {
      for (const cell of Array.isArray(row) ? row : []) {
        const candidate = normalizeCandidate(cell);
        if (candidate) return candidate;
      }
    }
  }
  return "";
}

export function resolveImageDocumentTitle(fileName, document) {
  const title = String(fileName || "").replace(/\.[^.]+$/, "").trim() || "扫描文档";
  if (!OPAQUE_FILE_TITLE.test(title)) return title;

  const paragraphs = Array.isArray(document?.paragraphs) ? document.paragraphs : [];
  const styles = Array.isArray(document?.paragraph_styles) ? document.paragraph_styles : [];
  const headingIndex = styles.findIndex((style, index) => (
    style === "title" && normalizeCandidate(paragraphs[index])
  ));
  const heading = headingIndex >= 0 ? normalizeCandidate(paragraphs[headingIndex]) : "";
  const firstParagraph = paragraphs.map(normalizeCandidate).find(Boolean) || "";
  return truncateTitle(heading || firstParagraph || firstTableText(document?.tables) || "扫描文档");
}
