const CACHE_NAME = 'attendance-v12';

const STATIC_ASSETS = [
  '/offline.html',
  '/static/css/style.css',
  '/static/css/bootstrap.min.css',
  '/static/js/app.js',
  '/static/js/bootstrap.bundle.min.js',
  '/static/js/html5-qrcode.min.js',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png'
];

const BYPASS_PATHS = [
  '/logout',
  '/login',
  '/student/login',
  '/teacher/login',
  '/generate',
  '/static/uploads/'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      for (const url of STATIC_ASSETS) {
        try {
          const r = await fetch(url, { cache: 'no-cache' });
          if (r.ok) {
            const rClone = r.clone();
            await cache.put(url, rClone);
          }
        } catch (e) {}
      }
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      self.clients.claim(),
      caches.keys().then(keys =>
        Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
      )
    ])
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
  if (url.origin !== location.origin) return;
  if (event.request.method !== 'GET') return;

  // تجاوز الـ cache لهذه المسارات
  const shouldBypass = BYPASS_PATHS.some(p =>
    url.pathname === p || url.pathname.startsWith(p.endsWith('/') ? p : `${p}/`)
  );
  if (shouldBypass) return;

  if (url.pathname.startsWith('/api/')) return;

  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .then(response => {
          if (response && response.ok && response.status === 200 && new URL(response.url).pathname === url.pathname) {
            const responseClone = response.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, responseClone));
          }
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(event.request);
          if (cached) return cached;
          const offline = await caches.match('/offline.html');
          if (offline) return offline;
          return new Response('Offline', { status: 503 });
        })
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        if (response && response.ok && response.status === 200) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, responseClone));
        }
        return response;
      }).catch(() => new Response('', { status: 503 }));
    })
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});

self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-attendance') {
    event.waitUntil(
      self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
        clients.forEach(client => client.postMessage({ type: 'sync-attendance' }));
      })
    );
  }
});

console.log('[SW] v12 loaded');
