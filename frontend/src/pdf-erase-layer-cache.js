const MIN_CACHED_POINTS = 512;
const MAX_CACHED_PIXELS = 4_000_000;

export function createEraseLayerCache(createCanvas) {
  let canvas = null;
  let renderedState = null;

  function releaseCanvas() {
    if (canvas) {
      canvas.width = 0;
      canvas.height = 0;
    }
    canvas = null;
  }

  function clear() {
    releaseCanvas();
    renderedState = null;
  }

  function draw(input) {
    const { pageIndex, strokes, width, height, pageWidth, pageHeight, pixelRatio, paint } = input;
    const isCurrent = renderedState
      && renderedState.pageIndex === pageIndex
      && renderedState.strokes === strokes
      && renderedState.strokeCount === strokes.length
      && renderedState.width === width
      && renderedState.height === height
      && renderedState.pageWidth === pageWidth
      && renderedState.pageHeight === pageHeight
      && renderedState.pixelRatio === pixelRatio;
    if (isCurrent) return renderedState.cacheable ? canvas : null;

    const pointCount = strokes.reduce((count, stroke) => count + stroke.points.length, 0);
    const baseState = { pageIndex, strokes, strokeCount: strokes.length, width, height, pageWidth, pageHeight, pixelRatio };
    if (pointCount < MIN_CACHED_POINTS || width * height > MAX_CACHED_PIXELS) {
      releaseCanvas();
      renderedState = { ...baseState, cacheable: false };
      return null;
    }

    if (!canvas) canvas = createCanvas();
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) {
      renderedState = { ...baseState, cacheable: false };
      return null;
    }
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, pageWidth, pageHeight);
    context.save();
    context.lineCap = "round";
    context.lineJoin = "round";
    paint(context, strokes);
    context.restore();
    renderedState = { ...baseState, cacheable: true };
    return canvas;
  }

  return { draw, clear };
}
