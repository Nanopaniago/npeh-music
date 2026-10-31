// Service worker: guarda a "casca" da app para abrir instantaneamente; a música e a API vão sempre à rede.
const CACHE = 'npeh-music-v2';
const SHELL = ['/', '/static/style.css?v=2', '/static/app.js?v=2', '/static/icons/icon.svg', '/static/icons/icon-192.png', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/stream')) return; // streaming direto (suporta Range)
  if (url.pathname.startsWith('/api/cover/')) {
    // capas: cache-first
    e.respondWith(caches.open(CACHE + '-covers').then(async (c) => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const r = await fetch(e.request);
      if (r.ok) c.put(e.request, r.clone());
      return r;
    }));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;
  // casca: network-first com fallback offline
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok && (SHELL.includes(url.pathname + url.search) || url.pathname === '/')) caches.open(CACHE).then((c) => c.put(e.request, r.clone()));
    return r;
  }).catch(() => caches.match(e.request).then((m) => m || caches.match('/'))));
});
