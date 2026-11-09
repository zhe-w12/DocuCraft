export function handleAssistantComposerKeydown(event, submit) {
  if (
    event?.key !== "Enter" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey ||
    event.isComposing || event.keyCode === 229
  ) return false;

  event.preventDefault();
  submit();
  return true;
}

export function resolveAssistantDraftOnConversationChange({
  previousConversationId,
  draftConversationId,
  currentDraft,
  nextDraft,
}) {
  const preserveUnassignedDraft = !previousConversationId && !draftConversationId;
  return {
    value: preserveUnassignedDraft
      ? String(currentDraft ?? "")
      : typeof nextDraft === "string" ? nextDraft : "",
    preserveUnassignedDraft,
  };
}
