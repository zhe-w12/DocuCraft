import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("exposes mobile workspace switches as pressed toggle buttons", () => {
  const source = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const controls = source.match(/<div v-if="documentPaneOpen" class="assistant-mobile-tabs"[\s\S]*?<\/div>/)?.[0] || "";

  assert.match(controls, /role="group" aria-label="工作区视图"/);
  assert.match(controls, /:aria-pressed="mobilePane === 'document'"/);
  assert.match(controls, /:aria-pressed="mobilePane === 'assistant'"/);
  assert.doesNotMatch(controls, /role="tablist"|role="tab"|aria-selected=/);
});
