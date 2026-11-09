import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const buildId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
const webRoot = dirname(fileURLToPath(import.meta.url));
const backendRoot = resolve(webRoot, "../backend");

function backendApiCompatibilityVersion() {
  const versionSource = readFileSync(resolve(backendRoot, "app/api_version.py"), "utf8");
  const match = versionSource.match(/^\s*API_COMPATIBILITY_VERSION\s*=\s*(\d+)\s*$/m);
  if (!match) throw new Error("无法读取后端 API 兼容版本");
  return Number(match[1]);
}

function backendSourceFingerprint() {
  const appRoot = resolve(backendRoot, "app");
  const pythonFiles = [];
  const collectPythonFiles = directory => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const entryPath = resolve(directory, entry.name);
      if (entry.isDirectory()) collectPythonFiles(entryPath);
      else if (entry.isFile() && entry.name.endsWith(".py")) pythonFiles.push(entryPath);
    }
  };
  collectPythonFiles(appRoot);
  pythonFiles.sort();
  pythonFiles.push(resolve(backendRoot, "requirements.txt"));

  const digest = createHash("sha256");
  for (const file of pythonFiles) {
    const relativePath = relative(backendRoot, file).split(sep).join("/");
    const source = readFileSync(file, "utf8").replace(/\r\n/g, "\n");
    digest.update(relativePath);
    digest.update("\0");
    digest.update(source);
    digest.update("\0");
  }
  return digest.digest("hex").slice(0, 16);
}

const backendFingerprint = backendSourceFingerprint();
const apiCompatibilityVersion = backendApiCompatibilityVersion();

function buildVersionPlugin() {
  return {
    name: "iscan-build-version",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "build-version.json",
        source: JSON.stringify({ buildId }),
      });
    },
  };
}

export default defineConfig({
  base: "./",
  define: {
    __ISCAN_BUILD_ID__: JSON.stringify(buildId),
    __ISCAN_BACKEND_FINGERPRINT__: JSON.stringify(backendFingerprint),
    __ISCAN_API_COMPATIBILITY_VERSION__: JSON.stringify(apiCompatibilityVersion),
  },
  plugins: [vue(), buildVersionPlugin()],
  server: { host: "127.0.0.1" },
  preview: { host: "127.0.0.1" },
  build: { outDir: "dist" },
});
