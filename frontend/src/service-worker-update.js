export function createServiceWorkerUpdateGate(reload) {
  let assistantBusy = false;
  let updatePending = false;
  let reloading = false;

  function reloadWhenIdle() {
    if (!updatePending || assistantBusy || reloading) return;
    updatePending = false;
    reloading = true;
    reload();
  }

  return {
    requestReload() {
      updatePending = true;
      reloadWhenIdle();
    },
    setAssistantBusy(value) {
      assistantBusy = Boolean(value);
      reloadWhenIdle();
    },
  };
}
