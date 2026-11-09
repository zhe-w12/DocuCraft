import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createStreamPersistenceScheduler } from "../src/stream-persistence.js";
import { createStreamRenderScheduler, DOCUMENT_STREAM_RENDER_DELAY_MS } from "../src/stream-render.js";

function createFakeTimers() {
  let nextId = 1;
  const callbacks = new Map();
  return {
    callbacks,
    setTimeout(callback, delay) {
      const id = nextId++;
      callbacks.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      callbacks.delete(id);
    },
  };
}

test("coalesces stream persistence into one bounded interval and flushes the latest state", () => {
  const timers = createFakeTimers();
  const persisted = [];
  const scheduler = createStreamPersistenceScheduler(value => persisted.push(value), 500, timers);

  scheduler.schedule("first chunk");
  scheduler.schedule("second chunk");
  scheduler.schedule("latest document state");
  assert.equal(timers.callbacks.size, 1);
  assert.equal([...timers.callbacks.values()][0].delay, 500);
  assert.deepEqual(persisted, []);

  [...timers.callbacks.values()][0].callback();
  assert.deepEqual(persisted, ["latest document state"]);
  assert.equal(timers.callbacks.size, 0);
});

test("flushes pending stream changes immediately and does not persist twice", () => {
  const timers = createFakeTimers();
  const persisted = [];
  const scheduler = createStreamPersistenceScheduler(value => persisted.push(value), 500, timers);

  scheduler.schedule("latest state");
  scheduler.flush();
  scheduler.flush();

  assert.deepEqual(persisted, ["latest state"]);
  assert.equal(timers.callbacks.size, 0);
});

test("cancel discards pending stream persistence", () => {
  const timers = createFakeTimers();
  const persisted = [];
  const scheduler = createStreamPersistenceScheduler(value => persisted.push(value), 500, timers);

  scheduler.schedule("unsaved state");
  scheduler.cancel();
  scheduler.flush();

  assert.deepEqual(persisted, []);
  assert.equal(timers.callbacks.size, 0);
});

test("coalesces rapid model deltas into regular live document renders", () => {
  const timers = createFakeTimers();
  const rendered = [];
  const scheduler = createStreamRenderScheduler(() => rendered.push("render"), 40, timers);

  scheduler.schedule();
  scheduler.schedule();
  scheduler.schedule();
  assert.equal(timers.callbacks.size, 1);
  assert.equal([...timers.callbacks.values()][0].delay, 40);
  assert.deepEqual(rendered, []);

  [...timers.callbacks.values()][0].callback();
  assert.deepEqual(rendered, ["render"]);
});

test("flushes the latest stream render immediately at completion", () => {
  const timers = createFakeTimers();
  const rendered = [];
  const scheduler = createStreamRenderScheduler(() => rendered.push("render"), 40, timers);

  scheduler.schedule();
  scheduler.flush();
  scheduler.flush();

  assert.deepEqual(rendered, ["render"]);
  assert.equal(timers.callbacks.size, 0);
});

test("cancels a pending stream render when it is no longer needed", () => {
  const timers = createFakeTimers();
  const rendered = [];
  const scheduler = createStreamRenderScheduler(() => rendered.push("render"), 40, timers);

  scheduler.schedule();
  scheduler.cancel();
  scheduler.flush();

  assert.deepEqual(rendered, []);
  assert.equal(timers.callbacks.size, 0);
});

test("throttles expensive document previews separately from the live message stream", () => {
  const timers = createFakeTimers();
  const scheduler = createStreamRenderScheduler(() => {}, DOCUMENT_STREAM_RENDER_DELAY_MS, timers);
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");

  scheduler.schedule();
  assert.equal(DOCUMENT_STREAM_RENDER_DELAY_MS, 100);
  assert.equal([...timers.callbacks.values()][0].delay, 100);
  assert.match(workspace, /createStreamRenderScheduler\(renderDocumentDraft, DOCUMENT_STREAM_RENDER_DELAY_MS\)/);
  assert.match(workspace, /if \(isChatWorkflow\) scheduleMessageScrollToLatest\(conversation\)/);
  assert.match(workspace, /documentStreamRender\.flush\(\)/);
  scheduler.cancel();
});
