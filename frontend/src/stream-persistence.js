export function createStreamPersistenceScheduler(persist, delayMs = 500, timers = globalThis) {
  let timer = null;
  let pendingValue;
  let hasPending = false;

  function flush() {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    if (!hasPending) return;
    const value = pendingValue;
    pendingValue = undefined;
    hasPending = false;
    persist(value);
  }

  function schedule(value) {
    pendingValue = value;
    hasPending = true;
    if (timer === null) timer = timers.setTimeout(flush, delayMs);
  }

  function cancel() {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    pendingValue = undefined;
    hasPending = false;
  }

  return { schedule, flush, cancel };
}
