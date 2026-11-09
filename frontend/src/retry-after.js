const RETRYABLE_STATUSES = new Set([429, 500, 502, 503, 504]);

function defaultWait(milliseconds) {
  return new Promise(resolve => globalThis.setTimeout(resolve, milliseconds));
}

export async function retryAfterRequest(
  request,
  {
    maxRetries = 10,
    wait = defaultWait,
    onRetry,
    shouldRetryWithoutCooldown = () => false,
    maxRetriesWithoutCooldown = 0,
    fallbackWaitMilliseconds = 1000,
  } = {},
) {
  let retries = 0;
  let retriesWithoutCooldown = 0;
  while (true) {
    try {
      return await request();
    } catch (error) {
      const status = Number(error?.status);
      const retryAfter = Number(error?.retryAfterSeconds);
      const canRetryWithoutCooldown = (
        retriesWithoutCooldown < maxRetriesWithoutCooldown
        && shouldRetryWithoutCooldown(error)
      );

      if (RETRYABLE_STATUSES.has(status) && Number.isFinite(retryAfter) && retryAfter > 0) {
        if (retries >= maxRetries) throw error;
        const seconds = Math.max(1, Math.ceil(retryAfter));
        onRetry?.(seconds, error);
        await wait(seconds * 1000);
        retries += 1;
        continue;
      }

      // A known transient failure can arrive wrapped as a non-retryable HTTP
      // status by an OCR gateway. Only explicitly selected errors may bypass
      // the status allowlist; ordinary 4xx responses still fail immediately.
      if (!canRetryWithoutCooldown) throw error;

      const delay = Number(fallbackWaitMilliseconds);
      if (!Number.isFinite(delay) || delay < 0) throw error;
      const seconds = Math.max(1, Math.ceil(delay / 1000));
      onRetry?.(seconds, error);
      await wait(delay);
      retriesWithoutCooldown += 1;
    }
  }
}
