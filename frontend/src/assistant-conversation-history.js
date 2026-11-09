export const ASSISTANT_SIDEBAR_HISTORY_LIMIT = 8;
export const ASSISTANT_DIALOG_HISTORY_PAGE_SIZE = 40;

function updatedAt(conversation) {
  const timestamp = Number(conversation?.updatedAt);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function orderAssistantConversationHistory(conversations) {
  if (!Array.isArray(conversations)) return [];
  return conversations
    .filter(conversation => conversation && Array.isArray(conversation.messages) && conversation.messages.length > 0)
    .slice()
    .sort((left, right) => updatedAt(right) - updatedAt(left));
}

export function sliceAssistantConversationHistory(conversations, limit) {
  if (!Array.isArray(conversations)) return [];
  const normalizedLimit = Number.isFinite(Number(limit))
    ? Math.max(0, Math.floor(Number(limit)))
    : 0;
  return conversations.slice(0, normalizedLimit);
}

export function nextAssistantConversationHistoryLimit(currentLimit, totalItems) {
  const normalizeCount = value => Number.isFinite(Number(value))
    ? Math.max(0, Math.floor(Number(value)))
    : 0;
  return Math.min(
    normalizeCount(totalItems),
    normalizeCount(currentLimit) + ASSISTANT_DIALOG_HISTORY_PAGE_SIZE,
  );
}
