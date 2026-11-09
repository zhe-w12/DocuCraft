import assert from "node:assert/strict";
import test from "node:test";
import viteConfig from "../vite.config.js";
import { isBackendVersionCompatible } from "../src/backend-version.js";

const expectedFingerprint = JSON.parse(viteConfig.define.__ISCAN_BACKEND_FINGERPRINT__);

test("accepts the exact backend source fingerprint embedded in the web build", () => {
  assert.equal(
    isBackendVersionCompatible({ source_fingerprint: expectedFingerprint }, expectedFingerprint),
    true,
  );
  assert.equal(
    isBackendVersionCompatible({ source_fingerprint: expectedFingerprint.toUpperCase() }, expectedFingerprint),
    true,
  );
});

test("rejects missing, malformed, or different backend fingerprints", () => {
  assert.equal(isBackendVersionCompatible(null, expectedFingerprint), false);
  assert.equal(isBackendVersionCompatible({}, expectedFingerprint), false);
  assert.equal(isBackendVersionCompatible({ source_fingerprint: "stale" }, expectedFingerprint), false);
  assert.equal(
    isBackendVersionCompatible({ source_fingerprint: "0000000000000000" }, expectedFingerprint),
    false,
  );
  assert.equal(isBackendVersionCompatible({ source_fingerprint: expectedFingerprint }, "invalid"), false);
});
