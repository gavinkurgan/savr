// Offline support: app shell is cached; pages are network-first with cache fallback,
// so anything you've opened works in the kitchen with no signal.
// Bump CACHE whenever cached pages could be stale in a misleading way (e.g. library changes).
const CACHE = 'savr-v4';
const SHELL = ['/', '/cart', '/recommend', '/plan', '/pantry', '/static/style.css', '/static/app.js', '/static/icon.svg'];
// Never cached or faked: the client uses these to tell whether the server is reachable.
const BYPASS = ['/healthz', '/api/'];

const OFFLINE_HTML = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline · savr</title>
<body style="font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;text-align:center">
<h1>Can't reach savr</h1>
<p>This page isn't saved for offline use, and the savr server isn't responding.</p>
<p>Check that the server is running, then <a href="">try again</a>.</p></body>`;

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (BYPASS.some((p) => url.pathname === p || url.pathname.startsWith(p))) return;
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    }).catch(async () => {
      const hit = await caches.match(req);
      if (hit) return hit;
      // Don't pass the cached home page off as a recipe page: say we're offline.
      if (req.mode === 'navigate') {
        return new Response(OFFLINE_HTML, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      }
      return Response.error();
    })
  );
});
