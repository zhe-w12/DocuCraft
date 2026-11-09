export const DOCUMENT_STREAM_RENDER_DELAY_MS = 100;

export function createStreamRenderScheduler(render, delayMs = 40, timers = globalThis) {
  let timer = null;
  let pending = false;

  function flush() {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    if (!pending) return;
    pending = false;
    render();
  }

  function schedule() {
    pending = true;
    if (timer === null) timer = timers.setTimeout(flush, delayMs);
  }

  function cancel() {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    pending = false;
  }

  return { schedule, flush, cancel };
}
