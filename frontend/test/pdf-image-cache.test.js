import assert from "node:assert/strict";
import test from "node:test";
import { createImagePromiseCache, loadImageBatches, loadImageFromObjectUrl, MAX_CONCURRENT_IMAGE_DECODES } from "../src/pdf-image-cache.js";

test("loads image batches concurrently in stable order with a bounded batch size", async () => {
  let active = 0;
  let maxActive = 0;
  const sources = Array.from({ length: MAX_CONCURRENT_IMAGE_DECODES * 2 + 1 }, (_, index) => index);
  const batches = [];

  for await (const batch of loadImageBatches(sources, async source => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await Promise.resolve();
    active -= 1;
    return `image-${source}`;
  })) {
    batches.push(batch);
  }

  assert.equal(maxActive, MAX_CONCURRENT_IMAGE_DECODES);
  assert.deepEqual(batches.map(batch => batch.start), [0, 4, 8]);
  assert.deepEqual(batches.flatMap(batch => batch.images), sources.map(source => `image-${source}`));
});

test("normalizes invalid image batch sizes to one worker", async () => {
  const batches = [];

  for await (const batch of loadImageBatches([1, 2], source => Promise.resolve(source), 0)) {
    batches.push(batch);
  }

  assert.deepEqual(batches, [
    { start: 0, images: [1] },
    { start: 1, images: [2] },
  ]);
});

test("deduplicates image decoding and reuses recently used signature images", async () => {
  const decoded = [];
  const cache = createImagePromiseCache(async (source) => {
    decoded.push(source);
    return { source };
  }, 2);

  const first = cache.load("data:image/png;base64,one");
  assert.equal(cache.load("data:image/png;base64,one"), first);
  await first;
  await cache.load("data:image/png;base64,two");
  await cache.load("data:image/png;base64,one");
  await cache.load("data:image/png;base64,three");
  await cache.load("data:image/png;base64,one");

  assert.deepEqual(decoded, [
    "data:image/png;base64,one",
    "data:image/png;base64,two",
    "data:image/png;base64,three",
  ]);
});

test("does not retain failed image decodes", async () => {
  let attempts = 0;
  const cache = createImagePromiseCache(async () => {
    attempts += 1;
    return null;
  });

  assert.equal(await cache.load("bad-image"), null);
  assert.equal(await cache.load("bad-image"), null);
  assert.equal(attempts, 2);
});

test("clears cached signature images when the document changes", async () => {
  let attempts = 0;
  const cache = createImagePromiseCache(async (source) => ({ source, id: ++attempts }));

  const first = await cache.load("signature");
  cache.clear();
  const second = await cache.load("signature");

  assert.notEqual(first, second);
  assert.equal(attempts, 2);
});

test("releases temporary image object URLs after decoding", async () => {
  const calls = [];
  const urlApi = {
    createObjectURL(file) {
      calls.push(["create", file.name]);
      return "blob:temporary-signature";
    },
    revokeObjectURL(url) {
      calls.push(["revoke", url]);
    },
  };
  const image = { width: 100, height: 40 };
  const result = await loadImageFromObjectUrl({ name: "signature.png" }, async (url) => {
    calls.push(["decode", url]);
    return image;
  }, urlApi);

  assert.equal(result, image);
  assert.deepEqual(calls, [
    ["create", "signature.png"],
    ["decode", "blob:temporary-signature"],
    ["revoke", "blob:temporary-signature"],
  ]);
});

test("releases a temporary image object URL even when decoding fails", async () => {
  const revoked = [];
  const urlApi = {
    createObjectURL() { return "blob:broken-signature"; },
    revokeObjectURL(url) { revoked.push(url); },
  };

  await assert.rejects(loadImageFromObjectUrl({}, async () => {
    throw new Error("decode failed");
  }, urlApi), /decode failed/);
  assert.deepEqual(revoked, ["blob:broken-signature"]);
});
