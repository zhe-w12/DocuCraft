import assert from "node:assert/strict";
import test from "node:test";
import { consumeAppUpdateView, persistAppUpdateView } from "../src/app-update-view.js";

const KEY = "iscan-app-update-view-v1";

function createStorage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
    values,
  };
}

test("restores and consumes the image tool after an app update reload", () => {
  const storage = createStorage();

  assert.equal(persistAppUpdateView(storage, { view: "tools", activeTool: "image" }), true);
  assert.deepEqual(consumeAppUpdateView(storage), { view: "tools", activeTool: "image" });
  assert.equal(storage.getItem(KEY), null);
  assert.equal(consumeAppUpdateView(storage), null);
});

test("restores other valid app views and normalizes their tool state", () => {
  const storage = createStorage();

  assert.equal(persistAppUpdateView(storage, { view: "history", activeTool: "pdf" }), true);
  assert.deepEqual(consumeAppUpdateView(storage), { view: "history", activeTool: "image" });
  assert.equal(persistAppUpdateView(storage, { view: "tools", activeTool: "pdf" }), true);
  assert.deepEqual(consumeAppUpdateView(storage), { view: "tools", activeTool: "pdf" });
});

test("discards malformed or unrecognized stored view values", () => {
  const storage = createStorage();
  storage.setItem(KEY, JSON.stringify({ view: "tools", activeTool: "unknown", secret: "ignored" }));

  assert.equal(consumeAppUpdateView(storage), null);
  assert.equal(storage.getItem(KEY), null);
  assert.equal(persistAppUpdateView(storage, { view: "unknown", activeTool: "image" }), false);
});

test("treats unavailable session storage as an optional optimization", () => {
  const storage = {
    getItem() { throw new Error("blocked"); },
    removeItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
  };

  assert.equal(consumeAppUpdateView(storage), null);
  assert.equal(persistAppUpdateView(storage, { view: "tools", activeTool: "image" }), false);
});
