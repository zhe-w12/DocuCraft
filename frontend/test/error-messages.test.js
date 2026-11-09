import assert from "node:assert/strict";
import test from "node:test";
import { formatImageImportFailure, friendlyError } from "../src/error-messages.js";

test("formats coded OCR failures with the request ID exactly once", () => {
  const requestId = "a".repeat(32);
  const error = Object.assign(new Error("图片识别服务暂时处理失败，请稍后重试"), {
    code: "OCR_NULL_CLOSE",
    requestId,
  });
  const message = formatImageImportFailure("scan.jpg", "服务端识别", error);

  assert.equal(
    message,
    `scan.jpg（服务端识别 · 编号 ${requestId}）：图片识别服务暂时处理失败，请稍后重试`,
  );
  assert.equal(message.split(requestId).length - 1, 1);
});

test("keeps local and server close failures readable without exposing raw errors or invalid IDs", () => {
  const error = new TypeError("Cannot read properties of null (reading 'close')");

  assert.equal(
    friendlyError(error),
    "浏览器暂时无法处理这张图片，请刷新后重试，或另存为 JPG/PNG 再导入",
  );
  assert.equal(
    formatImageImportFailure(
      "scan.jpg",
      "服务端识别",
      Object.assign(new TypeError(error.message), { requestId: "bad-id" }),
    ),
    "scan.jpg（服务端识别）：图片识别服务版本较旧，请更新服务器后重试",
  );
  assert.equal(
    formatImageImportFailure("scan.jpg", "服务端识别", error),
    "scan.jpg（服务端识别）：图片识别服务版本较旧，请更新服务器后重试",
  );
});

test("keeps the normal retry message for coded server-side cleanup failures", () => {
  const error = Object.assign(
    new TypeError("Cannot read properties of null (reading 'close')"),
    { code: "OCR_NULL_CLOSE" },
  );

  assert.equal(
    formatImageImportFailure("scan.jpg", "服务端识别", error),
    "scan.jpg（服务端识别）：图片识别服务暂时处理失败，请稍后重试",
  );
});
