"use strict";

const CACHE_NAME = "iscan-shell-v6";
const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./iscan-logo.png",
  "./site.webmanifest",
];
const SHELL_PATHS = new Set(SHELL_ASSETS.map(asset => new URL(asset, self.registration.scope).pathname));
const INDEX_URL = new URL("./index.html", self.registration.scope).href;
const ASSET_PATH = new URL("./assets/", self.registration.scope).pathname;

self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(SHELL_ASSETS);
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const cacheNames = await caches.keys();
    await Promise.all(cacheNames
      .filter(name => name.startsWith("iscan-shell-") && name !== CACHE_NAME)
      .map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

async function serveNavigation(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response.ok && response.type === "basic") await cache.put(request, response.clone());
    return response;
  } catch {
    return (await cache.match(INDEX_URL)) || Response.error();
  }
}

async function serveShellAsset(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response.ok && response.type === "basic") {
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    return (await cache.match(request, { ignoreSearch: true })) || Response.error();
  }
}

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  const scope = new URL(self.registration.scope);
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return;

  // Keep API calls, uploaded scans, and generated files out of the offline cache.
  if (url.pathname.includes("/api/v1/") || url.pathname.includes("/files/")) return;

  if (request.mode === "navigate") {
    event.respondWith(serveNavigation(request));
    return;
  }

  // Cache Vite's versioned assets together, including PDF module workers and
  // KaTeX fonts, so offline pages do not depend on resources missed by JS/CSS-only caching.
  if (SHELL_PATHS.has(url.pathname) || url.pathname.startsWith(ASSET_PATH)) {
    event.respondWith(serveShellAsset(request));
  }
});
