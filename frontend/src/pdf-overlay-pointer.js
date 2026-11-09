import { normalizePoint } from "./pdf-editor-geometry.js";

export function rebasePdfOverlayPointerAction(action, rect, pageSize) {
  if (!action) return;

  const point = normalizePoint(action.clientX, action.clientY, rect);
  if (action.type === "move") {
    action.startX = point.x;
    action.startY = point.y;
    action.x = action.item.x;
    action.y = action.item.y;
  } else if (action.type === "resize") {
    action.startX = point.x * pageSize.width;
    action.startY = point.y * pageSize.height;
    action.startWidth = action.item.width;
    action.startHeight = action.item.height;
  }
  action.rect = rect;
  return point;
}
