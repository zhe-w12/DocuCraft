const SUPPORTED_SERVER_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/bmp",
  "image/gif",
  "image/tiff",
]);

const DIRECT_UPLOAD_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/bmp",
  "image/gif",
  "image/tiff",
]);
const FALLBACK_DIRECT_UPLOAD_MAX_BYTES = 18 * 1024 * 1024;
const SERVER_MAX_IMAGE_PIXELS = 50_000_000;
const LOSSLESS_EDITOR_PREVIEW_MAX_PIXELS = 16_000_000;

const EXTENSION_MIME_TYPES = new Map([
  ["jpg", "image/jpeg"],
  ["jpeg", "image/jpeg"],
  ["png", "image/png"],
  ["webp", "image/webp"],
  ["bmp", "image/bmp"],
  ["gif", "image/gif"],
  ["tif", "image/tiff"],
  ["tiff", "image/tiff"],
]);

export function isImageFile(file) {
  return Boolean(file && (
    (typeof file.type === "string" && file.type.startsWith("image/"))
    || /\.(?:avif|bmp|gif|heic|heif|jpe?g|png|tiff?|webp)$/i.test(file.name || "")
  ));
}

function releaseDecodedImage(decoded) {
  try {
    decoded?.close?.();
  } catch {
    // A browser decoder may throw while releasing an ImageBitmap even though
    // the pixels were decoded successfully. Cleanup must not fail the import.
  }
}

async function decodeWithImageElement(file, environment) {
  const objectUrl = environment.URL.createObjectURL(file);
  const image = new environment.Image();
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    environment.URL.revokeObjectURL(objectUrl);
  };

  try {
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("图片解码失败"));
      image.src = objectUrl;
    });
    if (!(image.naturalWidth > 0 && image.naturalHeight > 0)) {
      throw new Error("图片尺寸无效");
    }
    return {
      source: image,
      width: image.naturalWidth,
      height: image.naturalHeight,
      kind: "image",
      close: release,
    };
  } catch (error) {
    release();
    throw error;
  }
}

async function decodeImage(file, environment) {
  if (typeof environment.createImageBitmap === "function") {
    try {
      const bitmap = await environment.createImageBitmap(file);
      if (bitmap && bitmap.width > 0 && bitmap.height > 0) {
        return {
          source: bitmap,
          width: bitmap.width,
          height: bitmap.height,
          kind: "bitmap",
          close: () => bitmap.close?.(),
        };
      }
      bitmap?.close?.();
    } catch {
      // Some browsers expose createImageBitmap but cannot decode HEIC/AVIF.
      // Fall through to their normal <img> decoder before rejecting the file.
    }
  }
  return decodeWithImageElement(file, environment);
}

function isNullCloseError(error) {
  return /Cannot read properties of null \(reading ['"]close['"]\)/i.test(
    String(error?.message || error || ""),
  );
}

function supportedMimeType(file) {
  const declaredType = String(file.type || "").toLowerCase();
  if (SUPPORTED_SERVER_MIME_TYPES.has(declaredType)) return declaredType;
  if (declaredType && declaredType !== "application/octet-stream") return "";
  const extension = String(file.name || "").split(".").at(-1).toLowerCase();
  return EXTENSION_MIME_TYPES.get(extension) || "";
}

function withMimeType(file, mimeType, environment) {
  if (String(file.type || "").toLowerCase() === mimeType) return file;
  return new environment.File([file], file.name, {
    type: mimeType,
    lastModified: file.lastModified,
  });
}

export function canUploadImageUnchanged(file, maxSourceUploadBytes) {
  const limit = Number(maxSourceUploadBytes);
  return Number.isSafeInteger(limit)
    && limit > 0
    && DIRECT_UPLOAD_MIME_TYPES.has(supportedMimeType(file))
    && Number(file?.size) > 0
    && Number(file.size) <= limit;
}

function encodeJpeg(canvas, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(result => result
      ? resolve(result)
      : reject(new Error("图片转换失败，请重试")), "image/jpeg", quality);
  });
}

function encodePng(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(result => result
      ? resolve(result)
      : reject(new Error("图片转换失败，请重试")), "image/png");
  });
}

function releaseCanvas(canvas) {
  if (!canvas) return;
  canvas.width = 0;
  canvas.height = 0;
}

function drawDecodedImage(decoded, environment) {
  if (decoded.width * decoded.height > SERVER_MAX_IMAGE_PIXELS) {
    throw new Error("图片分辨率超过识别上限；为保留原图清晰度，请先调整尺寸后重试");
  }

  const canvas = environment.document.createElement("canvas");
  try {
    canvas.width = decoded.width;
    canvas.height = decoded.height;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("无法准备图片，请重试");
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(decoded.source, 0, 0, canvas.width, canvas.height);
    return canvas;
  } catch (error) {
    releaseCanvas(canvas);
    throw error;
  }
}

async function encodeJpegWithinUploadLimit(canvas, maxUploadBytes) {
  for (const quality of [0.92, 0.86, 0.8, 0.74]) {
    const blob = await encodeJpeg(canvas, quality);
    if (blob.size <= maxUploadBytes) return blob;
  }
  throw new Error("转换后的图片仍超过上传限制；为保留原图尺寸，请先压缩文件后重试");
}

async function prepareImageWithDecoder(
  file,
  title,
  environment,
  useImageElement,
  maxUploadBytes,
  maxSourceUploadBytes,
  preserveDecodedRaster,
) {
  const serverMimeType = supportedMimeType(file);
  let decoded;
  try {
    decoded = useImageElement
      ? await decodeWithImageElement(file, environment)
      : await decodeImage(file, environment);
  } catch (error) {
    if (isNullCloseError(error)) throw error;
    // Pillow on the server can decode TIFF even though most browsers cannot.
    // Preserve it when it fits the upload limit so the full-resolution source
    // remains available for the editor instead of rejecting or downscaling it.
    if (serverMimeType === "image/tiff") {
      throw new Error("浏览器无法读取此 TIFF 图片，且文件超过上传限制；请先压缩后重试");
    }
    throw new Error("浏览器暂不支持读取此图片，请先将它转换为 JPG 或 PNG");
  }

  let canvas;
  try {
    try {
      canvas = drawDecodedImage(decoded, environment);
    } catch (bitmapError) {
      if (decoded.kind !== "bitmap") throw bitmapError;
      if (
        decoded.width * decoded.height > SERVER_MAX_IMAGE_PIXELS
        || bitmapError?.message === "无法准备图片，请重试"
      ) {
        throw bitmapError;
      }

      // Some browsers can decode a bitmap but fail while drawing it. Retry
      // through the regular <img> decoder before rejecting the import.
      releaseDecodedImage(decoded);
      decoded = null;
      try {
        decoded = await decodeWithImageElement(file, environment);
        canvas = drawDecodedImage(decoded, environment);
      } catch (imageError) {
        if (imageError?.message?.includes("图片分辨率超过识别上限")) throw imageError;
        throw new Error("浏览器暂时无法处理此图片，请先将它转换为 JPG 或 PNG");
      }
    }

    releaseDecodedImage(decoded);
    decoded = null;

    if (
      preserveDecodedRaster
      && canvas.width * canvas.height <= LOSSLESS_EDITOR_PREVIEW_MAX_PIXELS
    ) {
      try {
        const png = await encodePng(canvas);
        if (png.size <= maxSourceUploadBytes) {
          return new environment.File([png], `${title || "扫描文档"}.png`, {
            type: "image/png",
            lastModified: file.lastModified,
          });
        }
      } catch (error) {
        // Retry known browser cleanup failures through <img>; other PNG
        // encoder failures can still use the bounded JPEG fallback.
        if (isNullCloseError(error)) throw error;
      }
    }

    // Browser-only source formats must be rasterized before upload, but the
    // server can accept a larger source than the OCR provider's own limit.
    // Keep JPEG quality high up to the source cap; the backend creates its
    // separately bounded OCR preview from this preserved source.
    const jpegUploadLimit = preserveDecodedRaster ? maxSourceUploadBytes : maxUploadBytes;
    const blob = await encodeJpegWithinUploadLimit(canvas, jpegUploadLimit);
    return new environment.File([blob], `${title || "扫描文档"}.jpg`, {
      type: "image/jpeg",
      lastModified: file.lastModified,
    });
  } finally {
    releaseDecodedImage(decoded);
    releaseCanvas(canvas);
  }
}

export async function prepareImage(
  file,
  title = "扫描文档",
  environment = globalThis,
  uploadLimit = FALLBACK_DIRECT_UPLOAD_MAX_BYTES,
  sourceUploadLimit = uploadLimit,
) {
  const configuredUploadLimit = Number(uploadLimit);
  const maxUploadBytes = Number.isSafeInteger(configuredUploadLimit) && configuredUploadLimit > 0
    ? configuredUploadLimit
    : FALLBACK_DIRECT_UPLOAD_MAX_BYTES;
  const configuredSourceLimit = Number(sourceUploadLimit);
  const maxSourceUploadBytes = Number.isSafeInteger(configuredSourceLimit) && configuredSourceLimit > 0
    ? Math.max(maxUploadBytes, configuredSourceLimit)
    : maxUploadBytes;
  const serverMimeType = supportedMimeType(file);
  // This path is only reached when the original bytes cannot be uploaded as-is.
  // Preserve the decoded source within the larger source-image limit; the
  // backend makes its own bounded OCR preview from that source.
  const preserveDecodedRaster = true;
  if (canUploadImageUnchanged(file, maxSourceUploadBytes)) {
    // Preserve server-readable source bytes for the editor. OCR still has its
    // own smaller upload limit; the backend applies a separate bounded source cap.
    return withMimeType(file, serverMimeType, environment);
  }

  try {
    return await prepareImageWithDecoder(
      file,
      title,
      environment,
      false,
      maxUploadBytes,
      maxSourceUploadBytes,
      preserveDecodedRaster,
    );
  } catch (error) {
    if (
      !isNullCloseError(error)
      || typeof environment.createImageBitmap !== "function"
    ) {
      throw error;
    }

    // A browser can decode and draw an ImageBitmap but fail while encoding the
    // canvas. Rebuild it from <img> once before giving up on this import.
    try {
      return await prepareImageWithDecoder(
        file,
        title,
        environment,
        true,
        maxUploadBytes,
        maxSourceUploadBytes,
        preserveDecodedRaster,
      );
    } catch (retryError) {
      if (isNullCloseError(retryError)) {
        throw new Error("浏览器处理这张图片失败，请将它转换为 JPG 或 PNG 后重试");
      }
      throw retryError;
    }
  }
}
