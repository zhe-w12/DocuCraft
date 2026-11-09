const MAX_CANVAS_EDGE = 4096;
const MAX_CANVAS_PIXELS = 16_000_000;

export function releasePdfCanvas(canvas) {
  if (!canvas) return;
  canvas.width = 0;
  canvas.height = 0;
}

export function resolvePdfCanvasPixelRatio(width, height, devicePixelRatio = 1) {
  const safeWidth = Number(width);
  const safeHeight = Number(height);
  if (!Number.isFinite(safeWidth) || !Number.isFinite(safeHeight) || safeWidth <= 0 || safeHeight <= 0) return 1;

  const requestedRatio = Number(devicePixelRatio);
  const safeDeviceRatio = Number.isFinite(requestedRatio) && requestedRatio > 0 ? requestedRatio : 1;
  const requestedScale = Math.min(
    2,
    safeDeviceRatio,
    MAX_CANVAS_EDGE / Math.max(safeWidth, safeHeight),
    Math.sqrt(MAX_CANVAS_PIXELS / (safeWidth * safeHeight)),
  );
  const fits = (scale) => {
    const bitmapWidth = Math.ceil(safeWidth * scale);
    const bitmapHeight = Math.ceil(safeHeight * scale);
    return bitmapWidth <= MAX_CANVAS_EDGE
      && bitmapHeight <= MAX_CANVAS_EDGE
      && bitmapWidth * bitmapHeight <= MAX_CANVAS_PIXELS;
  };
  if (fits(requestedScale)) return requestedScale;

  let lower = 0;
  let upper = requestedScale;
  for (let index = 0; index < 40; index += 1) {
    const midpoint = (lower + upper) / 2;
    if (fits(midpoint)) lower = midpoint;
    else upper = midpoint;
  }
  return lower;
}
