export function orderDocumentHistory(items) {
  if (!Array.isArray(items)) return [];

  return items
    .map((item, index) => ({ item, index, timestamp: parseHistoryTimestamp(item?.createdAt) }))
    .sort((a, b) => {
      if (a.timestamp === null && b.timestamp !== null) return 1;
      if (b.timestamp === null && a.timestamp !== null) return -1;
      return (b.timestamp ?? 0) - (a.timestamp ?? 0) || a.index - b.index;
    })
    .map(entry => entry.item);
}

export function parseHistoryTimestamp(value) {
  if (value instanceof Date) {
    const timestamp = value.valueOf();
    return Number.isFinite(timestamp) ? timestamp : null;
  }
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || !value.trim()) return null;
  const normalized = value.trim();
  const numeric = Number(normalized);
  if (Number.isFinite(numeric)) return numeric;
  const parsed = Date.parse(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}
