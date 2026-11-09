import assert from "node:assert/strict";
import test from "node:test";
import { responseError, responseErrorMessage } from "../src/http-errors.js";

test("shows server detail and Retry-After on rate limit responses", async () => {
  const response = new Response(JSON.stringify({ detail: "图片识别请求过于频繁，请稍后再试" }), {
    status: 429,
    headers: { "Retry-After": "35" },
  });

  assert.equal(
    await responseErrorMessage(response, "请求失败"),
    "图片识别请求过于频繁，请稍后再试（35 秒后可重试）",
  );
});

test("formats validation details and leaves responses without retry hints alone", async () => {
  const response = new Response(JSON.stringify({ detail: [{ msg: "图片字段不能为空" }, { msg: "文件格式无效" }] }), {
    status: 422,
  });

  assert.equal(await responseErrorMessage(response, "请求失败"), "图片字段不能为空；文件格式无效");
});

test("uses plain-text bodies and the caller fallback for empty responses", async () => {
  assert.equal(await responseErrorMessage(new Response("服务暂不可用", { status: 503 })), "服务暂不可用");
  assert.equal(await responseErrorMessage(new Response(null, { status: 503 }), "导出失败"), "导出失败");
});

test("keeps HTTP status and Retry-After metadata for retry decisions", async () => {
  const error = await responseError(new Response(
    JSON.stringify({ detail: "服务繁忙" }),
    { status: 503, headers: {
      "Retry-After": "10",
      "X-Conversion-ID": "a".repeat(32),
      "X-Error-Code": "OCR_NULL_CLOSE",
    } },
  ));

  assert.equal(error.message, "服务繁忙（10 秒后可重试）");
  assert.equal(error.status, 503);
  assert.equal(error.retryAfterSeconds, 10);
  assert.equal(error.requestId, "a".repeat(32));
  assert.equal(error.code, "OCR_NULL_CLOSE");
});

test("ignores malformed conversion identifiers from error responses", async () => {
  const error = await responseError(new Response(
    JSON.stringify({ detail: "服务繁忙" }),
    { status: 503, headers: { "X-Conversion-ID": "not-an-id" } },
  ));

  assert.equal(error.requestId, undefined);
});
