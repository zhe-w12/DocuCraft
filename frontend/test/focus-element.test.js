import assert from "node:assert/strict";
import test from "node:test";
import { focusElement } from "../src/focus-element.js";

test("focuses a connected element without scrolling", () => {
  const calls = [];
  const element = {
    isConnected: true,
    focus(options) { calls.push(options); },
  };

  assert.equal(focusElement(element), true);
  assert.deepEqual(calls, [{ preventScroll: true }]);
});

test("retries without focus options when the browser rejects them", () => {
  const calls = [];
  const element = {
    isConnected: true,
    focus(options) {
      calls.push(arguments.length);
      if (options) throw new TypeError("focus options are unsupported");
    },
  };

  assert.equal(focusElement(element), true);
  assert.deepEqual(calls, [1, 0]);
});

test("does not focus a detached or non-focusable element", () => {
  let calls = 0;

  assert.equal(focusElement({ isConnected: false, focus() { calls += 1; } }), false);
  assert.equal(focusElement({ isConnected: true }), false);
  assert.equal(calls, 0);
});

test("reports failure if both focus attempts throw", () => {
  const element = {
    isConnected: true,
    focus() { throw new Error("cannot focus"); },
  };

  assert.equal(focusElement(element), false);
});
