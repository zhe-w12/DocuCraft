export const INITIAL_VISIBLE_ASSISTANT_MESSAGE_COUNT = 40;
export const ASSISTANT_MESSAGE_HISTORY_PAGE_SIZE = 40;

export function sliceLatestAssistantMessages(messages, limit) {
  if (!Array.isArray(messages)) return [];
  const normalizedLimit = Number.isFinite(Number(limit))
    ? Math.max(0, Math.floor(Number(limit)))
    : 0;
  return messages.slice(Math.max(0, messages.length - normalizedLimit));
}

export function nextAssistantMessageHistoryLimit(currentLimit, totalItems) {
  const normalizeCount = value => Number.isFinite(Number(value))
    ? Math.max(0, Math.floor(Number(value)))
    : 0;
  return Math.min(
    normalizeCount(totalItems),
    normalizeCount(currentLimit) + ASSISTANT_MESSAGE_HISTORY_PAGE_SIZE,
  );
}
