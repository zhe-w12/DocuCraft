import assert from "node:assert/strict";
import test from "node:test";
import { retryAfterRequest } from "../src/retry-after.js";

test("retries a rate-limited image request after the server cooldown", async () => {
  let attempts = 0;
  const waits = [];
  const notices = [];

  const result = await retryAfterRequest(async () => {
    attempts += 1;
    if (attempts === 1) {
      const error = new Error("too many requests");
      error.status = 429;
      error.retryAfterSeconds = 35;
      throw error;
    }
    return "recognized";
  }, {
    wait: async milliseconds => waits.push(milliseconds),
    onRetry: seconds => notices.push(seconds),
  });

  assert.equal(result, "recognized");
  assert.equal(attempts, 2);
  assert.deepEqual(waits, [35000]);
  assert.deepEqual(notices, [35]);
});

test("retries a temporary 503 only when the server supplies a cooldown", async () => {
  let attempts = 0;
  const result = await retryAfterRequest(async () => {
    attempts += 1;
    if (attempts === 1) {
      const error = new Error("busy");
      error.status = 503;
      error.retryAfterSeconds = 10;
      throw error;
    }
    return "ready";
  }, { wait: async () => {} });

  assert.equal(result, "ready");
  assert.equal(attempts, 2);
});

test("retries the known temporary image close failure once without a server cooldown", async () => {
  let attempts = 0;
  const waits = [];
  const notices = [];
  const result = await retryAfterRequest(async () => {
    attempts += 1;
    if (attempts === 1) {
      throw Object.assign(new Error("Cannot read properties of null (reading 'close')"), { status: 503 });
    }
    return "recognized";
  }, {
    shouldRetryWithoutCooldown: error => /reading 'close'/.test(error.message),
    maxRetriesWithoutCooldown: 1,
    fallbackWaitMilliseconds: 1000,
    wait: async milliseconds => waits.push(milliseconds),
    onRetry: seconds => notices.push(seconds),
  });

  assert.equal(result, "recognized");
  assert.equal(attempts, 2);
  assert.deepEqual(waits, [1000]);
  assert.deepEqual(notices, [1]);
});

test("retries a transient OCR service failure once when the server has no cooldown", async () => {
  let attempts = 0;
  const waits = [];
  const result = await retryAfterRequest(async () => {
    attempts += 1;
    if (attempts === 1) {
      throw Object.assign(new Error("自建 OCR 服务暂时不可用，请稍后重试"), {
        status: 503,
        code: "OCR_TRANSIENT",
      });
    }
    return "recognized";
  }, {
    shouldRetryWithoutCooldown: error => error?.code === "OCR_TRANSIENT",
    maxRetriesWithoutCooldown: 1,
    fallbackWaitMilliseconds: 1000,
    wait: async milliseconds => waits.push(milliseconds),
  });

  assert.equal(result, "recognized");
  assert.equal(attempts, 2);
  assert.deepEqual(waits, [1000]);
});

test("retries a known temporary image close failure for any server error status", async () => {
  for (const status of [500, 502, 503, 504]) {
    let attempts = 0;
    const result = await retryAfterRequest(async () => {
      attempts += 1;
      if (attempts === 1) {
        throw Object.assign(
          new Error("Cannot read properties of null (reading 'close')"),
          { status },
        );
      }
      return "recognized";
    }, {
      shouldRetryWithoutCooldown: error => (
        [500, 502, 503, 504].includes(Number(error?.status))
        && /Cannot read properties of null \(reading ['"]close['"]\)/i.test(error.message)
      ),
      maxRetriesWithoutCooldown: 1,
      wait: async () => {},
    });

    assert.equal(result, "recognized", `status ${status}`);
    assert.equal(attempts, 2, `status ${status}`);
  }
});

test("retries the known OCR cleanup error once when a gateway wraps it as a client error", async () => {
  let attempts = 0;
  const waits = [];
  const result = await retryAfterRequest(async () => {
    attempts += 1;
    if (attempts === 1) {
      throw Object.assign(
        new Error("Cannot read properties of null (reading 'close')"),
        { status: 400 },
      );
    }
    return "recognized";
  }, {
    shouldRetryWithoutCooldown: error => /Cannot read properties of null \(reading ['\"]close['\"]\)/i.test(error.message),
    maxRetriesWithoutCooldown: 1,
    fallbackWaitMilliseconds: 1000,
    wait: async milliseconds => waits.push(milliseconds),
  });

  assert.equal(result, "recognized");
  assert.equal(attempts, 2);
  assert.deepEqual(waits, [1000]);
});

test("does not retry an unrelated temporary response without a cooldown", async () => {
  let attempts = 0;
  await assert.rejects(retryAfterRequest(async () => {
    attempts += 1;
    throw Object.assign(new Error("service unavailable"), { status: 503 });
  }, {
    shouldRetryWithoutCooldown: error => /reading 'close'/.test(error.message),
    maxRetriesWithoutCooldown: 1,
    wait: async () => assert.fail("unrelated 503 should not retry"),
  }), /service unavailable/);
  assert.equal(attempts, 1);
});

test("does not retry permanent errors or rate limits without Retry-After", async () => {
  for (const status of [400, 500, 429]) {
    let attempts = 0;
    const error = Object.assign(new Error("request failed"), { status });
    await assert.rejects(
      retryAfterRequest(async () => {
        attempts += 1;
        throw error;
      }, { wait: async () => assert.fail("request should not wait") }),
      /request failed/,
    );
    assert.equal(attempts, 1);
  }
});

test("stops retrying after the configured number of cooldowns", async () => {
  let attempts = 0;
  let waits = 0;
  await assert.rejects(retryAfterRequest(async () => {
    attempts += 1;
    throw Object.assign(new Error("busy"), {
      status: 429,
      retryAfterSeconds: 1,
    });
  }, {
    maxRetries: 2,
    wait: async () => { waits += 1; },
  }), /busy/);

  assert.equal(attempts, 3);
  assert.equal(waits, 2);
});

test("ignores invalid cooldowns and honors long server cooldowns", async () => {
  const waits = [];
  for (const retryAfterSeconds of ["invalid", 0, -1]) {
    await assert.rejects(retryAfterRequest(async () => {
      throw Object.assign(new Error("busy"), {
        status: 429,
        retryAfterSeconds,
      });
    }, {
      wait: async milliseconds => waits.push(milliseconds),
    }), /busy/);
  }
  assert.deepEqual(waits, []);

  let attempts = 0;
  await retryAfterRequest(async () => {
    attempts += 1;
    if (attempts === 1) {
      throw Object.assign(new Error("busy"), {
        status: 429,
        retryAfterSeconds: 600,
      });
    }
    return "ok";
  }, {
    wait: async milliseconds => waits.push(milliseconds),
  });
  assert.deepEqual(waits, [600000]);
});
