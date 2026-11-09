import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("tells the assistant workspace when its page is visible", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");

  assert.match(app, /<AssistantWorkspace v-else :api-base="state\.api" :active="state\.view === 'assistant'" \/>/);
  assert.match(workspace, /active: \{ type: Boolean, default: true \}/);
});

test("skips transcript and document Markdown rendering while the workspace is hidden", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const renderedDocument = workspace.match(/const renderedDocument = computed\(\(\) => \{([\s\S]*?)\n\}\);/)?.[1] || "";

  assert.match(renderedDocument, /if \(!props\.active\) return EMPTY_RENDERED_DOCUMENT/);
  assert.match(workspace, /v-else-if="props\.active" class="assistant-message-content" v-html="renderAssistantMessage\(message\)"/);
  assert.match(workspace, /v-if="props\.active && \(assistantState\.documentContent \|\| editorMode === 'edit'\)" class="document-content-wrap"/);
});

test("does not keep an unused recent-conversation computation in the workspace", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");

  assert.doesNotMatch(workspace, /const recentConversations = computed\(/);
});

test("flushes changes and restores panel scroll positions when returning to the workspace", () => {
  const workspace = readFileSync(new URL("../src/AssistantWorkspace.vue", import.meta.url), "utf8");
  const activationWatch = workspace.match(/watch\(\(\) => props\.active, active => \{([\s\S]*?)\n\}\);/)?.[1] || "";

  assert.match(activationWatch, /flushPendingPersistence\(\)/);
  assert.match(activationWatch, /if \(editorMode\.value === "edit"\) refreshDocumentEditor\(\)/);
  assert.match(activationWatch, /restoreScrollPosition\(messageList\.value, inactiveMessageScrollTop, followMessageScroll\.value\)/);
  assert.match(activationWatch, /restoreScrollPosition\(documentPageScrollElement\.value, inactiveDocumentScrollTop, followDocumentScroll\.value\)/);
});
