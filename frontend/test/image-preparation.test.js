import assert from "node:assert/strict";
import test from "node:test";
import { canUploadImageUnchanged, isImageFile, prepareImage } from "../src/image-preparation.js";

class TestFile extends Blob {
  constructor(parts, name, options = {}) {
    super(parts, options);
    this.name = name;
    this.lastModified = options.lastModified ?? 0;
  }
}

function createEnvironment({
  bitmapFactory,
  imageLoads = true,
  imageWidth = 800,
  imageHeight = 600,
  onDraw,
  onBlob,
  blobFactory,
} = {}) {
  const revokedUrls = [];
  const drawSources = [];
  const encodedQualities = [];
  const encodedTypes = [];
  const encodedDimensions = [];
  const canvas = {
    width: 0,
    height: 0,
    getContext() {
      return {
        fillStyle: "",
        fillRect() {},
        drawImage(source) {
          drawSources.push(source);
          onDraw?.(source);
        },
      };
    },
    toBlob(callback, type, quality) {
      encodedQualities.push(quality);
      encodedTypes.push(type);
      encodedDimensions.push([canvas.width, canvas.height]);
      onBlob?.(canvas, type, quality);
      callback(blobFactory
        ? blobFactory(canvas, type, quality)
        : new Blob(["converted"], { type }));
    },
  };

  class ImageStub {
    naturalWidth = imageWidth;
    naturalHeight = imageHeight;

    set src(value) {
      this.url = value;
      queueMicrotask(() => (imageLoads ? this.onload : this.onerror)?.());
    }
  }

  const environment = {
    Image: ImageStub,
    File: TestFile,
    URL: {
      createObjectURL: () => "blob:test-image",
      revokeObjectURL: value => revokedUrls.push(value),
    },
    document: { createElement: () => canvas },
  };
  if (bitmapFactory) environment.createImageBitmap = bitmapFactory;
  return { environment, revokedUrls, drawSources, encodedQualities, encodedTypes, encodedDimensions, canvas };
}

test("sends a normal-sized server-readable image unchanged without decoding it", async () => {
  let decodeCount = 0;
  const file = new TestFile(["original"], "scan.jpg", {
    type: "image/jpeg",
    lastModified: 12,
  });
  const { environment } = createEnvironment({
    bitmapFactory: async () => { decodeCount += 1; },
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result, file);
  assert.equal(decodeCount, 0);
});

test("does not fail a required transcode when bitmap cleanup throws", async () => {
  const file = new TestFile(["heic"], "scan.heic", { type: "image/heic" });
  const { environment, canvas, encodedDimensions } = createEnvironment({
    bitmapFactory: async () => ({
      width: 1200,
      height: 900,
      close() { throw new TypeError("Cannot read properties of null (reading 'close')"); },
    }),
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result.type, "image/png");
  assert.deepEqual(encodedDimensions, [[1200, 900]]);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
});

test("rebuilds the image through the image element when bitmap PNG encoding fails", async () => {
  const file = new TestFile(["heic"], "scan.heic", { type: "image/heic" });
  let encodingCount = 0;
  let closeCount = 0;
  const { environment, drawSources, encodedDimensions, canvas } = createEnvironment({
    bitmapFactory: async () => ({
      width: 1200,
      height: 900,
      close() { closeCount += 1; },
    }),
    onBlob() {
      encodingCount += 1;
      if (encodingCount === 1) {
        throw new TypeError("Cannot read properties of null (reading 'close')");
      }
    },
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result.name, "scan.png");
  assert.equal(result.type, "image/png");
  assert.equal(encodingCount, 2);
  assert.equal(drawSources.length, 2);
  assert.deepEqual(encodedDimensions, [[1200, 900], [800, 600]]);
  assert.ok(drawSources[0]?.close);
  assert.ok(drawSources[1] instanceof environment.Image);
  assert.equal(closeCount, 1);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
});

test("reports a clear error and releases resources when image-element encoding also fails", async () => {
  const file = new TestFile(["heic"], "scan.heic", { type: "image/heic" });
  const { environment, drawSources, revokedUrls, canvas } = createEnvironment({
    bitmapFactory: async () => ({
      width: 1200,
      height: 900,
      close() {},
    }),
    onBlob() {
      throw new TypeError("Cannot read properties of null (reading 'close')");
    },
  });

  await assert.rejects(
    prepareImage(file, "scan", environment),
    /浏览器处理这张图片失败，请将它转换为 JPG 或 PNG 后重试/,
  );

  assert.equal(drawSources.length, 2);
  assert.ok(drawSources[1] instanceof environment.Image);
  assert.deepEqual(revokedUrls, ["blob:test-image"]);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
});

test("falls back to the image element when drawing a decoded bitmap fails", async () => {
  const file = new TestFile(["heic"], "scan.heic", { type: "image/heic" });
  let closeCount = 0;
  const { environment, drawSources, revokedUrls } = createEnvironment({
    bitmapFactory: async () => ({
      width: 1200,
      height: 900,
      close() { closeCount += 1; },
    }),
    onDraw(source) {
      if (typeof source?.close === "function") {
        throw new TypeError("Cannot read properties of null (reading 'close')");
      }
    },
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result.name, "scan.png");
  assert.equal(result.type, "image/png");
  assert.equal(drawSources.length, 2);
  assert.ok(drawSources[1] instanceof environment.Image);
  assert.equal(closeCount, 1);
  assert.deepEqual(revokedUrls, ["blob:test-image"]);
});

test("uses the browser image decoder when createImageBitmap cannot decode HEIC", async () => {
  const file = new TestFile(["heic"], "scan.heic", {
    type: "image/heic",
    lastModified: 13,
  });
  const { environment, revokedUrls, drawSources, encodedDimensions, canvas } = createEnvironment({
    bitmapFactory: async () => { throw new Error("unsupported format"); },
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result.name, "scan.png");
  assert.equal(result.type, "image/png");
  assert.equal(result.lastModified, 13);
  assert.deepEqual(encodedDimensions, [[800, 600]]);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
  assert.equal(drawSources.length, 1);
  assert.ok(drawSources[0] instanceof environment.Image);
  assert.deepEqual(revokedUrls, ["blob:test-image"]);
});

test("transcodes when createImageBitmap is unavailable and releases the object URL", async () => {
  const file = new TestFile(["avif"], "scan.avif", { type: "image/avif" });
  const { environment, revokedUrls } = createEnvironment({
    imageWidth: 2400,
    imageHeight: 1200,
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result.name, "scan.png");
  assert.equal(result.type, "image/png");
  assert.equal(result.size, "converted".length);
  assert.equal(revokedUrls.length, 1);
});

test("adds a server-readable MIME type when the browser leaves it blank", async () => {
  const file = new TestFile(["jpeg"], "scan.jpg");
  const { environment } = createEnvironment({
    bitmapFactory: async () => ({ width: 900, height: 700, close() {} }),
  });

  const result = await prepareImage(file, "scan", environment);

  assert.notEqual(result, file);
  assert.equal(result.name, "scan.jpg");
  assert.equal(result.type, "image/jpeg");
  assert.equal(result.size, file.size);
});

test("sends a large but uploadable image unchanged without allocating a canvas", async () => {
  let decodeCount = 0;
  const file = new TestFile(["large"], "large.png", { type: "image/png" });
  const { environment, canvas } = createEnvironment({
    bitmapFactory: async () => { decodeCount += 1; },
  });

  const result = await prepareImage(file, "large", environment);

  assert.equal(result, file);
  assert.equal(canvas.width, 0);
  assert.equal(decodeCount, 0);
});

test("sends a 19 MB server-readable image unchanged when the backend allows 20 MB", async () => {
  let decodeCount = 0;
  const file = new TestFile(["large"], "large.jpg", { type: "image/jpeg" });
  Object.defineProperty(file, "size", { value: 19 * 1024 * 1024 });
  const { environment, canvas } = createEnvironment({
    bitmapFactory: async () => { decodeCount += 1; },
  });

  const result = await prepareImage(file, "large", environment, 20 * 1024 * 1024);

  assert.equal(result, file);
  assert.equal(canvas.width, 0);
  assert.equal(decodeCount, 0);
});

test("preserves an original image above the OCR limit when the source upload limit allows it", async () => {
  let decodeCount = 0;
  const file = new TestFile(["original bytes"], "large.png", { type: "image/png" });
  Object.defineProperty(file, "size", { value: 19 * 1024 * 1024 });
  const { environment, canvas } = createEnvironment({
    bitmapFactory: async () => { decodeCount += 1; },
  });

  assert.equal(canUploadImageUnchanged(file, 20 * 1024 * 1024), true);
  const result = await prepareImage(
    file,
    "large",
    environment,
    10 * 1024 * 1024,
    20 * 1024 * 1024,
  );

  assert.equal(result, file);
  assert.equal(canvas.width, 0);
  assert.equal(decodeCount, 0);
});

test("preserves a server-readable TIFF when the browser cannot decode it", async () => {
  const file = new TestFile(["original tiff"], "scan.tiff", {
    type: "image/tiff",
    lastModified: 42,
  });
  let decodeCount = 0;
  const { environment, revokedUrls, canvas } = createEnvironment({
    bitmapFactory: async () => { decodeCount += 1; throw new Error("TIFF is unsupported by this browser"); },
  });

  const result = await prepareImage(file, "scan", environment);

  assert.equal(result, file);
  assert.equal(result.type, "image/tiff");
  assert.equal(result.lastModified, 42);
  assert.equal(canvas.width, 0, "unsupported TIFF should not be rasterized through a lossy canvas");
  assert.equal(decodeCount, 0);
  assert.deepEqual(revokedUrls, []);
});

test("hands TIFF pixel-limit validation to the server without browser decoding", async () => {
  const file = new TestFile(["original tiff"], "large.tif", { type: "image/tiff" });
  let decodeCount = 0;
  const { environment, canvas } = createEnvironment({
    bitmapFactory: async () => { decodeCount += 1; return { width: 10000, height: 6000 }; },
  });

  const result = await prepareImage(file, "large", environment);

  assert.equal(result, file);
  assert.equal(canvas.width, 0);
  assert.equal(decodeCount, 0);
});

test("rejects oversized images that need transcoding instead of silently downscaling", async () => {
  const file = new TestFile(["huge"], "huge.heic", { type: "image/heic" });
  const { environment, canvas } = createEnvironment({
    bitmapFactory: async () => ({ width: 10000, height: 6000, close() {} }),
  });

  await assert.rejects(
    prepareImage(file, "huge", environment),
    /分辨率超过识别上限/,
  );

  assert.equal(canvas.width, 0);
});

test("keeps unsupported phone images as lossless PNG at their original pixel dimensions", async () => {
  const file = new TestFile(["heic"], "phone.heic", { type: "image/heic" });
  const { environment, canvas, encodedQualities, encodedTypes, encodedDimensions } = createEnvironment({
    bitmapFactory: async () => ({ width: 4032, height: 3024, close() {} }),
  });

  const result = await prepareImage(file, "phone", environment);

  assert.deepEqual(encodedDimensions, [[4032, 3024]]);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
  assert.equal(result.name, "phone.png");
  assert.equal(result.type, "image/png");
  assert.deepEqual(encodedTypes, ["image/png"]);
  assert.deepEqual(encodedQualities, [undefined]);
});

test("falls back to bounded JPEG when a lossless PNG preview exceeds the source upload limit", async () => {
  const file = new TestFile(["heic"], "phone.heic", { type: "image/heic" });
  const { environment, encodedTypes } = createEnvironment({
    bitmapFactory: async () => ({ width: 1200, height: 900, close() {} }),
    blobFactory: (_canvas, type) => {
      const blob = new Blob(["encoded"], { type });
      if (type === "image/png") {
        Object.defineProperty(blob, "size", { value: 101 * 1024 * 1024 });
      }
      return blob;
    },
  });

  const result = await prepareImage(
    file,
    "phone",
    environment,
    20 * 1024 * 1024,
    100 * 1024 * 1024,
  );

  assert.equal(result.name, "phone.jpg");
  assert.equal(result.type, "image/jpeg");
  assert.deepEqual(encodedTypes, ["image/png", "image/jpeg"]);
});

test("keeps high JPEG quality up to the source limit for large browser-only images", async () => {
  const file = new TestFile(["heic"], "large.heic", { type: "image/heic" });
  const { environment, encodedQualities } = createEnvironment({
    bitmapFactory: async () => ({ width: 5000, height: 4000, close() {} }),
    blobFactory: (_canvas, type, quality) => {
      const blob = new Blob(["encoded"], { type });
      if (type === "image/jpeg" && quality === 0.92) {
        Object.defineProperty(blob, "size", { value: 22 * 1024 * 1024 });
      }
      return blob;
    },
  });

  const result = await prepareImage(
    file,
    "large",
    environment,
    20 * 1024 * 1024,
    100 * 1024 * 1024,
  );

  assert.equal(result.type, "image/jpeg");
  assert.deepEqual(encodedQualities, [0.92]);
});

test("reduces JPEG quality to keep full-resolution transcodes within the upload limit", async () => {
  const file = new TestFile(["heic"], "large.heic", { type: "image/heic" });
  const { environment, canvas, encodedQualities, encodedDimensions } = createEnvironment({
    bitmapFactory: async () => ({ width: 5000, height: 4000, close() {} }),
    blobFactory: (_canvas, type, quality) => {
      if (quality > 0.86) {
        const blob = new Blob(["oversized"], { type });
        Object.defineProperty(blob, "size", { value: 19 * 1024 * 1024 });
        return blob;
      }
      return new Blob(["compressed"], { type });
    },
  });

  const result = await prepareImage(file, "large", environment);

  assert.deepEqual(encodedDimensions, [[5000, 4000], [5000, 4000]]);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
  assert.deepEqual(encodedQualities, [0.92, 0.86]);
  assert.equal(result.type, "image/jpeg");
});

test("uses the backend upload limit when deciding whether a JPEG transcode fits", async () => {
  const file = new TestFile(["heic"], "large.heic", { type: "image/heic" });
  const { environment, encodedQualities } = createEnvironment({
    bitmapFactory: async () => ({ width: 5000, height: 4000, close() {} }),
    blobFactory: (_canvas, type) => {
      const blob = new Blob(["large"], { type });
      Object.defineProperty(blob, "size", { value: 19 * 1024 * 1024 });
      return blob;
    },
  });

  const result = await prepareImage(file, "large", environment, 20 * 1024 * 1024);

  assert.equal(result.type, "image/jpeg");
  assert.deepEqual(encodedQualities, [0.92]);
});

test("reports an explicit size error when a full-resolution transcode stays oversized", async () => {
  const file = new TestFile(["heic"], "too-large.heic", { type: "image/heic" });
  const { environment, canvas, encodedQualities, encodedDimensions } = createEnvironment({
    bitmapFactory: async () => ({ width: 6000, height: 7000, close() {} }),
    blobFactory: (_canvas, type) => {
      const blob = new Blob(["oversized"], { type });
      Object.defineProperty(blob, "size", { value: 19 * 1024 * 1024 });
      return blob;
    },
  });

  await assert.rejects(
    prepareImage(file, "too-large", environment),
    /仍超过上传限制/,
  );

  assert.deepEqual(encodedDimensions, [[6000, 7000], [6000, 7000], [6000, 7000], [6000, 7000]]);
  assert.deepEqual([canvas.width, canvas.height], [0, 0]);
  assert.deepEqual(encodedQualities, [0.92, 0.86, 0.8, 0.74]);
});

test("reports undecodable images clearly and revokes the failed decode URL", async () => {
  const file = new TestFile(["broken"], "broken.heic", { type: "image/heic" });
  const { environment, revokedUrls } = createEnvironment({
    bitmapFactory: async () => { throw new Error("unsupported format"); },
    imageLoads: false,
  });

  await assert.rejects(
    prepareImage(file, "broken", environment),
    /浏览器暂不支持读取此图片/,
  );
  assert.deepEqual(revokedUrls, ["blob:test-image"]);
});

test("recognizes image files by MIME or common extension", () => {
  assert.equal(isImageFile({ type: "image/heic", name: "photo" }), true);
  assert.equal(isImageFile({ type: "", name: "photo.tiff" }), true);
  assert.equal(isImageFile({ type: "application/pdf", name: "photo.pdf" }), false);
});
