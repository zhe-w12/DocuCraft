export function triggerBrowserDownload(href, filename, documentRef = globalThis.document) {
  const body = documentRef?.body;
  if (!body) throw new Error("当前页面无法开始下载");

  const anchor = documentRef.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
  }
}

export function downloadBlob(blob, filename, documentRef = globalThis.document) {
  const href = URL.createObjectURL(blob);
  try {
    triggerBrowserDownload(href, filename, documentRef);
  } catch (error) {
    URL.revokeObjectURL(href);
    throw error;
  }
  globalThis.setTimeout(() => URL.revokeObjectURL(href), 30_000);
}
