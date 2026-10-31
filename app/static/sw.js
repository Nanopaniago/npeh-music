// Service worker do Pobrefy:
//  - guarda a "casca" da app para abrir instantaneamente (e offline);
//  - capas: cache primeiro;
//  - música: se a faixa foi guardada offline serve-a do cache (com suporte a Range, para avançar/recuar), senão vai à rede.
const SHELL_CACHE = 'pobrefy-shell-v4';
const COVERS = 'pobrefy-covers-v1';
const AUDIO = 'pobrefy-audio-v1';      // o mesmo nome que a app usa em OFF.cache
const SHELL = ['/', '/static/style.css?v=3', '/static/app.js?v=3', '/static/icons/icon.svg', '/static/icons/icon-192.png', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  const keep = [SHELL_CACHE, COVERS, AUDIO];
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => !keep.includes(k)).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

async function serveStream(req) {
  const path = new URL(req.url).pathname;
  const hit = await (await caches.open(AUDIO)).match(path);
  if (!hit) {
    try { return await fetch(req); } catch { return Response.error(); }
  }
  const range = req.headers.get('range');
  if (!range) return hit;
  const buf = await hit.arrayBuffer();
  const size = buf.byteLength;
  const m = /bytes=(\d*)-(\d*)/.exec(range);
  if (!m) return hit;
  let start = m[1] ? parseInt(m[1], 10) : 0;
  let end = m[2] ? parseInt(m[2], 10) : size - 1;
  if (!m[1] && m[2]) { start = Math.max(0, size - parseInt(m[2], 10)); end = size - 1; }
  end = Math.min(end, size - 1);
  if (start > end) return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${size}` } });
  return new Response(buf.slice(start, end + 1), {
    status: 206,
    headers: {
      'Content-Type': hit.headers.get('Content-Type') || 'audio/mpeg',
      'Content-Range': `bytes ${start}-${end}/${size}`,
      'Content-Length': String(end - start + 1),
      'Accept-Ranges': 'bytes',
    },
  });
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/stream/')) { e.respondWith(serveStream(e.request)); return; }
  if (url.pathname.startsWith('/api/cover/')) {
    e.respondWith(caches.open(COVERS).then(async (c) => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      try {
        const r = await fetch(e.request);
        if (r.ok) c.put(e.request, r.clone());
        return r;
      } catch { return Response.error(); }
    }));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;
  // casca: rede primeiro, com a cópia guardada como alternativa offline
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok && (SHELL.includes(url.pathname + url.search) || url.pathname === '/')) caches.open(SHELL_CACHE).then((c) => c.put(e.request, r.clone()));
    return r;
  }).catch(() => caches.match(e.request).then((m) => m || caches.match('/'))));
});
