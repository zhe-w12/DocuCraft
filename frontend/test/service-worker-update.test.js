import assert from "node:assert/strict";
import test from "node:test";
import { createServiceWorkerUpdateGate } from "../src/service-worker-update.js";

test("waits for an active assistant response before reloading a new app shell", () => {
  let reloads = 0;
  const gate = createServiceWorkerUpdateGate(() => { reloads += 1; });

  gate.setAssistantBusy(true);
  gate.requestReload();
  assert.equal(reloads, 0);

  gate.setAssistantBusy(false);
  assert.equal(reloads, 1);
  gate.setAssistantBusy(false);
  assert.equal(reloads, 1);
});

test("reloads immediately when the assistant is idle", () => {
  let reloads = 0;
  const gate = createServiceWorkerUpdateGate(() => { reloads += 1; });

  gate.requestReload();
  gate.requestReload();

  assert.equal(reloads, 1);
});
