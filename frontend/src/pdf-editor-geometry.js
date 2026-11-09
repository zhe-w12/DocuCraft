export function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

export function normalizePoint(clientX, clientY, rect) {
  return {
    x: clamp((clientX - rect.left) / rect.width, 0, 1),
    y: clamp((clientY - rect.top) / rect.height, 0, 1),
  };
}

export function toCanvasPoint(point, width, height) {
  return { x: point.x * width, y: point.y * height };
}

export function rotatePointAroundCenter(x, y, centerX, centerY, angleDegrees) {
  const radians = angleDegrees * Math.PI / 180;
  const cosine = Math.cos(radians);
  const sine = Math.sin(radians);
  const dx = x - centerX;
  const dy = y - centerY;
  return {
    x: centerX + dx * cosine - dy * sine,
    y: centerY + dx * sine + dy * cosine,
  };
}

export function inverseRotatePointAroundCenter(x, y, centerX, centerY, angleDegrees) {
  return rotatePointAroundCenter(x, y, centerX, centerY, -angleDegrees);
}

export function rotatedBounds(width, height, angleDegrees) {
  const radians = angleDegrees * Math.PI / 180;
  const cosine = Math.abs(Math.cos(radians));
  const sine = Math.abs(Math.sin(radians));
  return {
    width: cosine * width + sine * height,
    height: sine * width + cosine * height,
  };
}
