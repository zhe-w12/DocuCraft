const WINDOWS_DEVICE_NAME = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i;

export function safeDownloadStem(value, fallback = "DocuCraft-文档") {
  let stem = String(value ?? "")
    .trim()
    .replace(/[\u0000-\u001f\u007f-\u009f<>:"/\\|?*]/g, "-")
    .replace(/\s+/g, " ")
    .replace(/[. ]+$/g, "");
  stem = Array.from(stem).slice(0, 80).join("").replace(/[. ]+$/g, "");
  if (!stem) stem = fallback;
  return WINDOWS_DEVICE_NAME.test(stem) ? `_${stem}` : stem;
}
