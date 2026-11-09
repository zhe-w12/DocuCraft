export function createId(cryptoApi = globalThis.crypto) {
  const randomUUID = cryptoApi?.randomUUID;
  if (typeof randomUUID === "function") {
    try {
      const id = randomUUID.call(cryptoApi);
      if (typeof id === "string" && id) return id;
    } catch { /* Fall back when a browser exposes but cannot call randomUUID. */ }
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
