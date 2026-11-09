import assert from "node:assert/strict";
import test from "node:test";
import { isSafeForAutomaticAppUpdate } from "../src/app-update-policy.js";

const idleImageTool = {
  hasAppUpdate: true,
  assistantReady: true,
  assistantSaveFailed: false,
  view: "tools",
  activeTool: "image",
  busy: false,
  failedImageCount: 0,
  saving: false,
  editorOpen: false,
  pdfWorkspaceOpen: false,
  reloadRequested: false,
};

test("automatically applies an update on an idle image tool page", () => {
  assert.equal(isSafeForAutomaticAppUpdate(idleImageTool), true);
});

test("waits until the app and local assistant storage are ready", () => {
  assert.equal(isSafeForAutomaticAppUpdate({ ...idleImageTool, assistantReady: false }), false);
  assert.equal(isSafeForAutomaticAppUpdate({ ...idleImageTool, assistantSaveFailed: true }), false);
});

test("preserves failed image imports so they can be retried", () => {
  assert.equal(isSafeForAutomaticAppUpdate({ ...idleImageTool, failedImageCount: 1 }), false);
});

test("waits while processing, saving or showing an editor", () => {
  for (const blockedState of [
    { busy: true },
    { saving: true },
    { editorOpen: true },
    { pdfWorkspaceOpen: true },
  ]) {
    assert.equal(isSafeForAutomaticAppUpdate({ ...idleImageTool, ...blockedState }), false);
  }
});

test("does not apply updates outside the image tool or when none is pending", () => {
  for (const blockedState of [
    { hasAppUpdate: false },
    { view: "assistant" },
    { activeTool: "pdf" },
    { reloadRequested: true },
  ]) {
    assert.equal(isSafeForAutomaticAppUpdate({ ...idleImageTool, ...blockedState }), false);
  }
});
