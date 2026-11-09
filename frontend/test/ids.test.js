import assert from "node:assert/strict";
import test from "node:test";
import { createId } from "../src/ids.js";

test("uses crypto.randomUUID when the browser provides a callable method", () => {
  const cryptoApi = {
    prefix: "test-",
    randomUUID() { return `${this.prefix}uuid`; },
  };

  assert.equal(createId(cryptoApi), "test-uuid");
});

test("falls back when randomUUID is absent or exposed as a non-function", () => {
  for (const cryptoApi of [{}, { randomUUID: "unavailable" }, { randomUUID: null }]) {
    assert.match(createId(cryptoApi), /^\d+-[a-z0-9]+$/);
  }
});

test("falls back when a browser randomUUID implementation throws", () => {
  assert.match(createId({ randomUUID() { throw new TypeError("unavailable"); } }), /^\d+-[a-z0-9]+$/);
});
