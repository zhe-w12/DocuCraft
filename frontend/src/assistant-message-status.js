export function resolveAssistantMessageStatus(message, state) {
  if (message?.status !== "streaming") return message?.status || "";

  const belongsToActiveRequest = Boolean(
    state?.sending
      && state.activeConversationId
      && state.activeConversationId === state.requestConversationId
      && state.streamingMessageId
      && state.streamingMessageId === message.id,
  );
  return belongsToActiveRequest ? "streaming" : "interrupted";
}
