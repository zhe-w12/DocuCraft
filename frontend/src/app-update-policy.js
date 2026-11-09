export function isSafeForAutomaticAppUpdate({
  hasAppUpdate,
  assistantReady,
  assistantSaveFailed,
  view,
  activeTool,
  filePickerOpen,
  updatePreparing,
  busy,
  failedImageCount,
  saving,
  editorOpen,
  pdfWorkspaceOpen,
  reloadRequested,
} = {}) {
  return Boolean(
    hasAppUpdate
      && assistantReady
      && !assistantSaveFailed
      && view === "tools"
      && activeTool === "image"
      && !filePickerOpen
      && !updatePreparing
      && !busy
      && !failedImageCount
      && !saving
      && !editorOpen
      && !pdfWorkspaceOpen
      && !reloadRequested,
  );
}
