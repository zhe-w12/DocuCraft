export function isNearScrollEnd(element, threshold = 88) {
  if (!element) return false;
  const distance = element.scrollHeight - element.clientHeight - element.scrollTop;
  return distance <= Math.max(0, threshold);
}

export function scrollAdjustmentForAnchor(previousTop, currentTop) {
  const previous = Number(previousTop);
  const current = Number(currentTop);
  return Number.isFinite(previous) && Number.isFinite(current) ? current - previous : 0;
}

export function restoreScrollPosition(element, previousTop, followLatest = false) {
  if (!element) return;
  const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
  const savedTop = Number(previousTop);
  element.scrollTop = followLatest
    ? maxScrollTop
    : Math.min(Number.isFinite(savedTop) ? Math.max(0, savedTop) : 0, maxScrollTop);
}
