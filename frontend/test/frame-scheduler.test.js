import assert from "node:assert/strict";
import test from "node:test";
import { createFrameScheduler } from "../src/frame-scheduler.js";

function createFakeFrames() {
  let nextId = 1;
  const callbacks = new Map();
  return {
    callbacks,
    requestAnimationFrame(callback) {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    },
    cancelAnimationFrame(id) {
      callbacks.delete(id);
    },
    runNext() {
      const [id, callback] = callbacks.entries().next().value || [];
      if (!callback) return;
      callbacks.delete(id);
      callback();
    },
  };
}

test("coalesces rapid pointer updates into one paint per animation frame", () => {
  const frames = createFakeFrames();
  const paints = [];
  const scheduler = createFrameScheduler(() => paints.push("paint"), frames);

  scheduler.schedule();
  scheduler.schedule();
  scheduler.schedule();
  assert.equal(frames.callbacks.size, 1);

  frames.runNext();
  assert.deepEqual(paints, ["paint"]);
  assert.equal(frames.callbacks.size, 0);
});

test("flushes a scheduled paint immediately and only once", () => {
  const frames = createFakeFrames();
  const paints = [];
  const scheduler = createFrameScheduler(() => paints.push("paint"), frames);

  scheduler.schedule();
  scheduler.flush();
  scheduler.flush();

  assert.deepEqual(paints, ["paint"]);
  assert.equal(frames.callbacks.size, 0);
});

test("cancels pending paints when the PDF editor closes", () => {
  const frames = createFakeFrames();
  const paints = [];
  const scheduler = createFrameScheduler(() => paints.push("paint"), frames);

  scheduler.schedule();
  scheduler.cancel();
  frames.runNext();
  scheduler.flush();

  assert.deepEqual(paints, []);
  assert.equal(frames.callbacks.size, 0);
});
