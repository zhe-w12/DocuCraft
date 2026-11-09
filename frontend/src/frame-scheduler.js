export function createFrameScheduler(render, frames = globalThis) {
  let frame = null;
  let pending = false;

  function flush() {
    if (frame !== null) frames.cancelAnimationFrame(frame);
    frame = null;
    if (!pending) return;
    pending = false;
    render();
  }

  function schedule() {
    pending = true;
    if (frame !== null) return;
    frame = frames.requestAnimationFrame(() => {
      frame = null;
      if (!pending) return;
      pending = false;
      render();
    });
  }

  function cancel() {
    if (frame !== null) frames.cancelAnimationFrame(frame);
    frame = null;
    pending = false;
  }

  return { schedule, flush, cancel };
}
