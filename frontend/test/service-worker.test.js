import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

test("caches all versioned app assets while excluding API and user files", async () => {
  const source = await readFile(new URL("../public/service-worker.js", import.meta.url), "utf8");
  const listeners = new Map();
  const cachedPaths = [];
  const cache = {
    async match() { return null; },
    async put(request) { cachedPaths.push(new URL(request.url).pathname); },
  };
  const self = {
    registration: { scope: "https://iscan.test/" },
    clients: { claim: async () => {} },
    skipWaiting: async () => {},
    addEventListener: (name, handler) => listeners.set(name, handler),
  };
  const context = {
    self,
    URL,
    Response: { error: () => ({ error: true }) },
    caches: { open: async () => cache },
    fetch: async () => ({ ok: true, type: "basic", clone() { return this; } }),
  };
  vm.runInNewContext(source, context);

  async function dispatch(url) {
    let response;
    listeners.get("fetch")({
      request: { method: "GET", mode: "cors", url },
      respondWith(value) { response = value; },
    });
    if (response) await response;
    return Boolean(response);
  }

  assert.equal(await dispatch("https://iscan.test/assets/pdf.worker.min.mjs"), true);
  assert.equal(await dispatch("https://iscan.test/assets/KaTeX_Main-Regular.woff2"), true);
  assert.equal(await dispatch("https://iscan.test/assets/index.css"), true);
  assert.equal(await dispatch("https://iscan.test/api/v1/documents/1"), false);
  assert.equal(await dispatch("https://iscan.test/files/private.pdf"), false);
  assert.deepEqual(cachedPaths, [
    "/assets/pdf.worker.min.mjs",
    "/assets/KaTeX_Main-Regular.woff2",
    "/assets/index.css",
  ]);
});
