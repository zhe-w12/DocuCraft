const EDITABLE_DOCUMENT_HEADING_SELECTOR = "h1, h2, h3, h4, h5, h6";

export function outlineEditableDocument(editor) {
  if (!editor?.querySelectorAll) return [];

  return Array.from(editor.querySelectorAll(EDITABLE_DOCUMENT_HEADING_SELECTOR), (heading, index) => {
    const id = `iscan-document-heading-${index + 1}`;
    heading.id = id;
    return {
      id,
      title: String(heading.textContent || "").replace(/\s+/g, " ").trim() || "未命名标题",
      level: Number(String(heading.tagName || "").slice(1)) || 1,
    };
  });
}

export function hasSameDocumentOutline(left, right) {
  return Array.isArray(left)
    && Array.isArray(right)
    && left.length === right.length
    && left.every((item, index) => (
      item.id === right[index].id
      && item.title === right[index].title
      && item.level === right[index].level
    ));
}
