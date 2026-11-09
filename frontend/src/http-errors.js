function validationDetail(detail) {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (Array.isArray(detail)) {
    return detail
      .map(item => typeof item?.msg === "string" ? item.msg : "请求内容无效")
      .join("；");
  }
  return "";
}

export async function responseErrorMessage(response, fallback = "请求失败，请稍后重试") {
  let message = fallback;
  let body = "";
  try {
    body = await response.text();
  } catch { /* Keep the caller's fallback when the response body cannot be read. */ }

  if (body) {
    try {
      const parsed = JSON.parse(body);
      message = validationDetail(parsed?.detail) || body.trim() || fallback;
    } catch {
      message = body.trim() || fallback;
    }
  }

  const retryAfter = Number(response.headers?.get?.("Retry-After"));
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    message += `（${Math.ceil(retryAfter)} 秒后可重试）`;
  }
  return message;
}

export async function responseError(response, fallback = "请求失败，请稍后重试") {
  const error = new Error(await responseErrorMessage(response, fallback));
  error.status = Number(response?.status) || 0;
  const retryAfter = Number(response?.headers?.get?.("Retry-After"));
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    error.retryAfterSeconds = retryAfter;
  }
  const conversionId = response?.headers?.get?.("X-Conversion-ID");
  if (typeof conversionId === "string" && /^[a-f\d]{32}$/i.test(conversionId)) {
    error.requestId = conversionId;
  }
  const errorCode = response?.headers?.get?.("X-Error-Code");
  if (typeof errorCode === "string" && /^[A-Z\d_]{1,64}$/.test(errorCode)) {
    error.code = errorCode;
  }
  return error;
}
