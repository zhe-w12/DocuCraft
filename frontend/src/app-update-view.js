const APP_UPDATE_VIEW_KEY = "iscan-app-update-view-v1";

function normalizeAppUpdateView(value) {
  if (!value || typeof value !== "object") return null;
  if (value.view === "tools" && ["image", "pdf"].includes(value.activeTool)) {
    return { view: "tools", activeTool: value.activeTool };
  }
  if (["assistant", "history"].includes(value.view)) {
    return { view: value.view, activeTool: "image" };
  }
  return null;
}

export function persistAppUpdateView(storage, value) {
  const view = normalizeAppUpdateView(value);
  if (!storage || !view) return false;
  try {
    storage.setItem(APP_UPDATE_VIEW_KEY, JSON.stringify(view));
    return true;
  } catch {
    return false;
  }
}

export function consumeAppUpdateView(storage) {
  if (!storage) return null;
  try {
    const serialized = storage.getItem(APP_UPDATE_VIEW_KEY);
    storage.removeItem(APP_UPDATE_VIEW_KEY);
    return serialized ? normalizeAppUpdateView(JSON.parse(serialized)) : null;
  } catch {
    try { storage.removeItem(APP_UPDATE_VIEW_KEY); } catch { /* Storage can be unavailable. */ }
    return null;
  }
}
