export const MAX_CONCURRENT_IMAGE_DECODES = 4;

export function createImagePromiseCache(decodeImage, maxEntries = 12) {
  const entries = new Map();
  const limit = Math.max(1, Math.floor(Number(maxEntries) || 1));

  function load(source) {
    if (entries.has(source)) {
      const cached = entries.get(source);
      entries.delete(source);
      entries.set(source, cached);
      return cached;
    }

    const promise = Promise.resolve()
      .then(() => decodeImage(source))
      .catch(() => null)
      .then((image) => {
        if (!image && entries.get(source) === promise) entries.delete(source);
        return image;
      });
    entries.set(source, promise);
    while (entries.size > limit) entries.delete(entries.keys().next().value);
    return promise;
  }

  function clear() {
    entries.clear();
  }

  return { load, clear };
}

export async function* loadImageBatches(items, loadImage, batchSize = MAX_CONCURRENT_IMAGE_DECODES) {
  if (!Array.isArray(items)) return;
  const limit = Number.isFinite(Number(batchSize))
    ? Math.max(1, Math.floor(Number(batchSize)))
    : MAX_CONCURRENT_IMAGE_DECODES;

  for (let start = 0; start < items.length; start += limit) {
    const batch = items.slice(start, start + limit);
    yield { start, images: await Promise.all(batch.map(loadImage)) };
  }
}

export async function loadImageFromObjectUrl(file, decodeImage, urlApi = URL) {
  const objectUrl = urlApi.createObjectURL(file);
  try {
    return await decodeImage(objectUrl);
  } finally {
    urlApi.revokeObjectURL(objectUrl);
  }
}
