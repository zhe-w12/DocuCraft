export function createAssistantMessageMarkdownRenderer({ render, requestMathRenderer, isRendererReady }) {
  const cache = new WeakMap();

  return message => {
    const content = String(message?.content || "");
    const rendererReady = Boolean(isRendererReady());
    const cached = message && typeof message === "object" ? cache.get(message) : null;
    if (cached?.content === content && cached.rendererReady === rendererReady) return cached.html;

    requestMathRenderer(content);
    const html = render(content);
    if (message && typeof message === "object") {
      cache.set(message, { content, rendererReady, html });
    }
    return html;
  };
}
