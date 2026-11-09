export function focusElement(element, { preventScroll = true } = {}) {
  if (!element || element.isConnected === false || typeof element.focus !== "function") return false;

  try {
    element.focus({ preventScroll });
    return true;
  } catch {
    try {
      element.focus();
      return true;
    } catch {
      return false;
    }
  }
}
