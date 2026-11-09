export async function copyTextToClipboard(text, environment = globalThis) {
  const value = String(text ?? "");
  if (!value) throw new Error("没有可复制的内容");

  let clipboard;
  try {
    clipboard = environment.navigator?.clipboard;
  } catch { /* Some browsers restrict clipboard access in insecure contexts. */ }
  try {
    if (typeof clipboard?.writeText === "function") {
      await clipboard.writeText(value);
      return;
    }
  } catch { /* Fall back for insecure contexts and clipboard permission failures. */ }

  const document = environment.document;
  if (!document?.body || typeof document.createElement !== "function" || typeof document.execCommand !== "function") {
    throw new Error("浏览器不支持剪贴板复制");
  }

  const activeElement = document.activeElement;
  const selection = document.getSelection?.();
  const savedRanges = [];
  if (selection) {
    for (let index = 0; index < selection.rangeCount; index += 1) {
      try { savedRanges.push(selection.getRangeAt(index).cloneRange()); } catch { /* Ignore stale selection ranges. */ }
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  Object.assign(textarea.style, {
    position: "fixed",
    top: "0",
    left: "-9999px",
    width: "1px",
    height: "1px",
    padding: "0",
    border: "0",
    opacity: "0",
    fontSize: "16px",
  });

  document.body.appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, value.length);
    if (!document.execCommand("copy")) throw new Error("浏览器拒绝复制操作");
  } finally {
    textarea.remove();
    if (activeElement?.isConnected) {
      try { activeElement.focus({ preventScroll: true }); } catch { /* Ignore focus restoration errors. */ }
    }
    if (selection && savedRanges.length) {
      try {
        selection.removeAllRanges();
        for (const range of savedRanges) selection.addRange(range);
      } catch { /* Ignore selection restoration errors. */ }
    }
  }
}
