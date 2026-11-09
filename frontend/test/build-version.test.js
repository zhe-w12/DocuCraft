import assert from "node:assert/strict";
import test from "node:test";
import viteConfig from "../vite.config.js";

test("writes the same build id into the public version marker and app bundle", () => {
  const plugin = viteConfig.plugins.find(item => item.name === "iscan-build-version");
  assert.ok(plugin, "the production build version plugin is registered");

  let emittedAsset;
  plugin.generateBundle.call({ emitFile: asset => { emittedAsset = asset; } });

  assert.equal(emittedAsset.fileName, "build-version.json");
  assert.deepEqual(JSON.parse(emittedAsset.source), {
    buildId: JSON.parse(viteConfig.define.__ISCAN_BUILD_ID__),
  });
});

test("embeds a valid backend source fingerprint in the app bundle", () => {
  assert.match(
    JSON.parse(viteConfig.define.__ISCAN_BACKEND_FINGERPRINT__),
    /^[a-f\d]{16}$/,
  );
});
