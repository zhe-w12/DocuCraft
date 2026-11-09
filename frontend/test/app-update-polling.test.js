import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("checks for app updates only while the page is visible", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const mounted = app.match(/onMounted\(\(\) => \{([\s\S]*?)\n\}\);/)?.[1] || "";
  const visibleHandler = app.match(/function checkForAppUpdateWhenVisible\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const onlineHandler = app.match(/function checkForAppUpdateWhenOnline\(\) \{([\s\S]*?)\n\}/)?.[1] || "";

  assert.match(mounted, /checkForAppUpdateWhenVisible\(\)/);
  assert.match(mounted, /setInterval\(checkForAppUpdateWhenVisible, 60_000\)/);
  assert.match(mounted, /addEventListener\("visibilitychange", checkForAppUpdateWhenVisible\)/);
  assert.match(visibleHandler, /window\.document\.visibilityState === "visible"/);
  assert.match(onlineHandler, /void checkApi\(\)/);
  assert.match(onlineHandler, /void checkForAppUpdate\(\)/);
  assert.doesNotMatch(mounted, /void checkForAppUpdate\(\)|setInterval\(checkForAppUpdate,/);
});

test("does not leave update controls interactive above an open document modal", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");

  assert.match(app, /<aside v-if="hasAppUpdate && !state\.editorDocument && !state\.pdfWorkspaceOpen" class="app-update-notice"/);
});

test("automatically refreshes an outdated idle image import page without interrupting edits", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const autoUpdate = app.match(/const canAutoApplyImageToolUpdate = computed\(\(\) => ([\s\S]*?)\n\}\)\);/)?.[1] || "";
  const watcher = app.match(/watch\(canAutoApplyImageToolUpdate, shouldApply => \{([\s\S]*?)\n\}, \{ flush: "post" \}\);/)?.[1] || "";

  assert.match(autoUpdate, /isSafeForAutomaticAppUpdate\(/);
  assert.match(autoUpdate, /hasAppUpdate: hasAppUpdate\.value/);
  assert.match(autoUpdate, /assistantReady: assistantState\.ready/);
  assert.match(autoUpdate, /assistantSaveFailed: assistantState\.localSaveFailed/);
  assert.match(autoUpdate, /failedImageCount: state\.failedImageCount/);
  assert.match(autoUpdate, /editorOpen: Boolean\(state\.editorDocument\)/);
  assert.match(watcher, /if \(shouldApply\) void applyAppUpdate\(\)/);
});

test("preserves only the current safe page for an accepted app update", () => {
  const app = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  const persistence = app.match(/function persistCurrentViewForUpdate\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const applyUpdate = app.match(/async function applyAppUpdate\(\) \{([\s\S]*?)\n\}/)?.[1] || "";
  const serviceWorkerReload = app.match(/const onServiceWorkerControllerChange = \(\) => \{([\s\S]*?)\n\};/)?.[1] || "";

  assert.match(persistence, /persistAppUpdateView\(window\.sessionStorage/);
  assert.match(app, /activeTool: restoredAppUpdateView\?\.activeTool \|\| "image"/);
  assert.match(app, /view: restoredAppUpdateView\?\.view \|\| "assistant"/);
  assert.match(applyUpdate, /persistCurrentViewForUpdate\(\)/);
  assert.match(applyUpdate, /await flushAssistantPersistence\(\)/);
  assert.match(serviceWorkerReload, /void checkForAppUpdate\(\)/);
  assert.doesNotMatch(serviceWorkerReload, /persistCurrentViewForUpdate|requestReload/);
});
