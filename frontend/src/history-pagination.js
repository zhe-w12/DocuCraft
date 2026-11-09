export const HISTORY_PAGE_SIZE = 40;

export function sliceHistoryPage(items, limit) {
  if (!Array.isArray(items)) return [];
  const normalizedLimit = Number.isFinite(Number(limit))
    ? Math.max(0, Math.floor(Number(limit)))
    : 0;
  return items.slice(0, normalizedLimit);
}

export function nextHistoryPageLimit(currentLimit, totalItems, pageSize = HISTORY_PAGE_SIZE) {
  const normalizeCount = value => Number.isFinite(Number(value))
    ? Math.max(0, Math.floor(Number(value)))
    : 0;
  const total = normalizeCount(totalItems);
  const current = normalizeCount(currentLimit);
  const step = Math.max(1, normalizeCount(pageSize));
  return Math.min(total, current + step);
}
