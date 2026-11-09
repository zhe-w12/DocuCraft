export function shouldOpenDocumentPane(mode, lastAssistantWorkflow) {
  if (mode === "chat" || mode === "plan") return false;
  return !(mode === "auto" && lastAssistantWorkflow === "chat");
}

export function latestAssistantMessage(messages) {
  if (!Array.isArray(messages)) return null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === "assistant") return message;
  }
  return null;
}

export function latestDocumentMessageId(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === "assistant" && message.workflow && !["chat", "plan"].includes(message.workflow)) {
      return message.id || null;
    }
  }
  return null;
}
