export async function shareLinkWithFallback({ url, share, copy, manualCopy }) {
  if (typeof share === "function") {
    try {
      await share();
      return "shared";
    } catch (error) {
      if (error?.name === "AbortError") return "cancelled";
    }
  }

  try {
    await copy(url);
    return "copied";
  } catch {
    await manualCopy(url);
    return "manual";
  }
}
