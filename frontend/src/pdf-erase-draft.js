export function stageEraseStroke(draft, stroke) {
  draft.push(stroke);
}

export function appendErasePoint(stroke, point, rect, minDistancePx = 2) {
  const previous = stroke.points.at(-1);
  if (!previous) {
    stroke.points.push({ x: point.x, y: point.y });
    return true;
  }

  const deltaX = (point.x - previous.x) * rect.width;
  const deltaY = (point.y - previous.y) * rect.height;
  const distanceSquared = deltaX ** 2 + deltaY ** 2;
  const minimumDistance = Math.max(0, minDistancePx);
  if (distanceSquared === 0 || distanceSquared < minimumDistance ** 2) return false;

  stroke.points.push({ x: point.x, y: point.y });
  return true;
}

export function commitEraseDraft(draft, pageState) {
  if (!draft.length) return false;
  pageState.strokes.push(...draft.splice(0));
  return true;
}

export function discardEraseDraft(draft) {
  draft.length = 0;
}
