import { createId } from "./ids.js";

const REQUEST_TYPE = "iscan:collect-document";
const RESPONSE_TYPE = "iscan:editor-document";
const DIRTY_TYPE = "iscan:editor-dirty";

export function isEditorFrameMessage(event, frame) {
  return Boolean(
    frame?.contentWindow
    && event?.source === frame.contentWindow
    && event.origin === "null"
    && [RESPONSE_TYPE, DIRTY_TYPE].includes(event.data?.type),
  );
}

export function requestEditorDocument(frame, windowRef = globalThis.window, timeoutMs = 3000) {
  const target = frame?.contentWindow;
  if (!target || !windowRef?.addEventListener || typeof target.postMessage !== "function") {
    return Promise.reject(new Error("编辑器不可用"));
  }

  const requestId = createId();
  return new Promise((resolve, reject) => {
    let timer;
    const cleanup = () => {
      windowRef.removeEventListener("message", onMessage);
      windowRef.clearTimeout(timer);
    };
    const onMessage = event => {
      if (
        event.source !== target
        || event.origin !== "null"
        || event.data?.type !== RESPONSE_TYPE
        || event.data.requestId !== requestId
      ) return;
      cleanup();
      if (typeof event.data.payload !== "string") reject(new Error("编辑器返回内容无效"));
      else resolve(event.data.payload);
    };

    windowRef.addEventListener("message", onMessage);
    timer = windowRef.setTimeout(() => {
      cleanup();
      reject(new Error("编辑器响应超时"));
    }, timeoutMs);
    try {
      target.postMessage({ type: REQUEST_TYPE, requestId }, "*");
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}
