import { normalizePoint, toCanvasPoint } from "./pdf-editor-geometry.js";

export function appendSignaturePoint(stroke, point, width, height, minDistancePx = 1) {
  const points = stroke.points || (stroke.points = []);
  const previous = points[points.length - 1];
  if (!previous) {
    points.push({ x: point.x, y: point.y });
    return true;
  }

  const deltaX = (point.x - previous.x) * width;
  const deltaY = (point.y - previous.y) * height;
  const distanceSquared = deltaX ** 2 + deltaY ** 2;
  const minimumDistance = Math.max(0, minDistancePx);
  if (distanceSquared === 0 || distanceSquared < minimumDistance ** 2) return false;

  points.push({ x: point.x, y: point.y });
  return true;
}

export function rebaseSignaturePointer(pointer, rect) {
  const point = normalizePoint(pointer.clientX, pointer.clientY, rect);
  pointer.rect = rect;
  pointer.point = point;
  appendSignaturePoint(pointer.stroke, point, rect.width, rect.height, 0);
  return point;
}

export function drawSignatureStrokes(context, strokes, width, height) {
  context.clearRect(0, 0, width, height);
  context.save();
  context.lineCap = "round";
  context.lineJoin = "round";
  context.strokeStyle = "#172033";
  context.fillStyle = "#172033";

  for (const stroke of strokes) {
    const points = stroke.points || [];
    if (!points.length) continue;
    context.lineWidth = Number(stroke.width) || 1;
    if (points.length === 1) {
      const point = toCanvasPoint(points[0], width, height);
      context.beginPath();
      context.arc(point.x, point.y, context.lineWidth / 2, 0, Math.PI * 2);
      context.fill();
      continue;
    }

    context.beginPath();
    points.forEach((point, index) => {
      const mapped = toCanvasPoint(point, width, height);
      if (index === 0) context.moveTo(mapped.x, mapped.y);
      else context.lineTo(mapped.x, mapped.y);
    });
    context.stroke();
  }

  context.restore();
}
