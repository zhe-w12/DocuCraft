import assert from "node:assert/strict";
import test from "node:test";
import { drawSignatureImage, removeLightSignatureBackground } from "../src/pdf-signature-image.js";

test("removes white scan backgrounds while preserving dark ink and colored details", () => {
  const imageData = { data: new Uint8ClampedArray([
    255, 255, 255, 255,
    220, 220, 220, 255,
    30, 30, 30, 255,
    30, 100, 220, 255,
  ]) };

  removeLightSignatureBackground(imageData);

  assert.equal(imageData.data[3], 0, "paper white becomes transparent");
  assert.ok(imageData.data[7] > 0 && imageData.data[7] < 255, "anti-aliased ink gets a soft alpha edge");
  assert.deepEqual([...imageData.data.slice(8, 12)], [30, 30, 30, 255], "dark signature ink remains opaque");
  assert.deepEqual([...imageData.data.slice(12, 16)], [30, 100, 220, 255], "colored strokes are preserved");
});

test("samples a light gray image background and makes it transparent", () => {
  const data = new Uint8ClampedArray(5 * 3 * 4);
  for (let offset = 0; offset < data.length; offset += 4) {
    data.set([238, 238, 238, 255], offset);
  }
  data.set([12, 12, 12, 255], (1 * 5 + 1) * 4);
  data.set([218, 218, 218, 255], (1 * 5 + 2) * 4);
  const imageData = { data, width: 5, height: 3 };

  removeLightSignatureBackground(imageData);

  assert.equal(data[3], 0, "sampled gray paper becomes transparent");
  const inkOffset = (1 * 5 + 1) * 4;
  assert.deepEqual([...data.slice(inkOffset, inkOffset + 4)], [12, 12, 12, 255], "dark ink remains intact");
  const edgeOffset = (1 * 5 + 2) * 4;
  assert.ok(data[edgeOffset + 3] > 0 && data[edgeOffset + 3] < 255, "gray anti-aliased ink keeps a soft edge");
});

test("removes medium gray paper without erasing anti-aliased strokes", () => {
  const data = new Uint8ClampedArray(5 * 3 * 4);
  for (let offset = 0; offset < data.length; offset += 4) {
    data.set([150, 150, 150, 255], offset);
  }
  const inkOffset = (1 * 5 + 1) * 4;
  const edgeOffset = (1 * 5 + 2) * 4;
  data.set([12, 12, 12, 255], inkOffset);
  data.set([124, 124, 124, 255], edgeOffset);

  removeLightSignatureBackground({ data, width: 5, height: 3 });

  assert.equal(data[3], 0, "medium gray paper becomes transparent");
  assert.deepEqual([...data.slice(inkOffset, inkOffset + 4)], [12, 12, 12, 255]);
  assert.ok(data[edgeOffset + 3] > 0 && data[edgeOffset + 3] < 255, "stroke edge keeps partial alpha");
});

test("draws signatures centered with their stored rotation, size and opacity", () => {
  const calls = [];
  const alphaStack = [];
  const context = {
    save() { alphaStack.push(this.globalAlpha); calls.push(["save"]); },
    restore() { this.globalAlpha = alphaStack.pop(); calls.push(["restore"]); },
    translate(...args) { calls.push(["translate", ...args]); },
    rotate(value) { calls.push(["rotate", value]); },
    drawImage(...args) { calls.push(["drawImage", ...args]); },
    globalAlpha: 1,
  };
  const image = { name: "signature" };
  const signature = { x: 0.2, y: 0.3, width: 0.4, height: 0.1, opacity: 0.6, rotation: 90 };

  drawSignatureImage(context, image, signature, 500, 800);

  assert.deepEqual(calls, [
    ["save"],
    ["translate", 200, 280],
    ["rotate", Math.PI / 2],
    ["drawImage", image, -100, -40, 200, 80],
    ["restore"],
  ]);
  assert.equal(context.globalAlpha, 1, "restoring the canvas state also restores opacity");
});
