function validationDetail(detail) {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    return detail
      .map(item => typeof item?.msg === "string" ? item.msg : "请求内容无效")
      .join("；");
  }
  return "";
}

export function friendlyError(error) {
  if (error?.name === "AbortError") return "请求超时，请稍后重试";
  if (error?.name === "QuotaExceededError") return "浏览器存储空间不足，请下载文件后清理历史";
  if (error?.name === "SecurityError") return "浏览器阻止了本地存储，请检查隐私设置";
  const text = String(error?.message || error || "未知错误");
  if (text.includes("Failed to fetch")) return "无法连接后端，请检查接口地址或 HTTPS 配置";
  if (error?.code === "OCR_NULL_CLOSE") {
    return "图片识别服务暂时处理失败，请稍后重试";
  }
  if (/Cannot read properties of null \(reading ['\"]close['\"]\)/i.test(text)) {
    return error?.requestId
      ? "图片识别服务暂时处理失败，请稍后重试"
      : "浏览器暂时无法处理这张图片，请刷新后重试，或另存为 JPG/PNG 再导入";
  }
  try {
    const parsed = JSON.parse(text);
    const detail = validationDetail(parsed?.detail);
    if (detail) return detail;
  } catch { /* The server may return plain text. */ }
  return text.slice(0, 180);
}

export function formatImageImportFailure(fileName, failureStage, error) {
  const requestId = /^[a-f\d]{32}$/i.test(String(error?.requestId || ""))
    ? ` · 编号 ${error.requestId}`
    : "";
  const closeFailure = /Cannot read properties of null \(reading ['"]close['"]\)/i.test(
    String(error?.message || error || ""),
  );
  const isServerCloseFailure = closeFailure && failureStage === "服务端识别";
  const detail = isServerCloseFailure
    ? error?.code === "OCR_NULL_CLOSE"
      ? "图片识别服务暂时处理失败，请稍后重试"
      : "图片识别服务版本较旧，请更新服务器后重试"
    : friendlyError(error);
  return `${fileName}（${failureStage}${requestId}）：${detail}`;
}
