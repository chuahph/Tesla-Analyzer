/* Tesla Analyzer service worker.
 *
 * Uses runtime caching (no hard-coded precache list) so the same file works
 * whether the app is served from "/" (self-hosted) or "/Tesla-Analyzer/"
 * (GitHub Pages). Strategy:
 *   - navigations & data  -> network-first, fall back to cache when offline
 *   - other assets        -> cache-first, fall back to network
 */
const CACHE = "tesla-analyzer-v85"; // bump to invalidate cached assets on update

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isData(url) {
  return url.pathname.includes("/api/") || /summary-\d+\.json$|demo\.json$/.test(url.pathname);
}

// App UI files change often — fetch them network-first so a single reload picks
// up a new deploy. Heavy, rarely-changing bundles (vendor/, icons) stay
// cache-first for speed/offline.
function isAppAsset(url) {
  return /\.(css|js)$/.test(url.pathname) && !url.pathname.includes("/vendor/");
}

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    // For navigations, fall back to any cached page so the app still opens.
    if (request.mode === "navigate") {
      const any = await cache.match("index.html") || await cache.match("./");
      if (any) return any;
    }
    throw err;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  const fresh = await fetch(request);
  if (fresh && fresh.status === 200) cache.put(request, fresh.clone());
  return fresh;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // don't touch the Chart.js CDN etc.

  if (request.mode === "navigate" || isData(url) || isAppAsset(url)) {
    event.respondWith(networkFirst(request));
  } else {
    event.respondWith(cacheFirst(request));
  }
});
