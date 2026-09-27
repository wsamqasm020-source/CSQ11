const CACHE_NAME = 'attendance-v17';

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
  '/teacher/login'
];

const OFFLINE_FALLBACK_PATH = '/offline.html';
const LOGOUT_PATHS = ['/logout', '/teacher/logout', '/student/logout'];

function shouldBypass(pathname) {
  return BYPASS_PATHS.some((p) => {
    if (pathname === p) return true;
    if (p.endsWith('/')) return pathname.startsWith(p);
    return pathname === p || pathname.startsWith(`${p}/`);
  });
}

async function clearPrivateCache() {
  const cache = await caches.open(CACHE_NAME);
  const requests = await cache.keys();
  const publicPaths = new Set([...STATIC_ASSETS, '/static/manifest.json']);
  await Promise.all(requests.map((request) => {
    const pathname = new URL(request.url).pathname;
    const publicLogo = pathname.startsWith('/static/uploads/dept_logo_');
    return publicPaths.has(pathname) || publicLogo ? Promise.resolve() : cache.delete(request);
  }));
}

self.addEventListener('install', (event) => {
  self.skipWaiting();

  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      await Promise.all(
        STATIC_ASSETS.map(async (url) => {
          try {
            const response = await fetch(url, { cache: 'no-cache' });
            if (response && response.ok) {
              await cache.put(url, response.clone());
            }
          } catch (error) {
            console.warn('[SW] Cache skip:', url, error);
          }
        })
      );
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      await self.clients.claim();
      const cacheNames = await caches.keys();
      await Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    })()
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
  if (url.origin !== location.origin) return;

  if (LOGOUT_PATHS.includes(url.pathname)) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        await clearPrivateCache();
        return response;
      } catch (error) {
        return new Response('تعذر تسجيل الخروج دون اتصال', {
          status: 503,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      }
    })());
    return;
  }

  if (shouldBypass(url.pathname)) return;

  if (url.pathname.startsWith('/api/')) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (
          response &&
          response.ok &&
          response.status === 200 &&
          !response.redirected &&
          response.headers.get('content-type')?.includes('application/json')
        ) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(request, response.clone());
        }
        return response;
      } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;
        return new Response(JSON.stringify({ success: false, message: 'غير متصل ولا توجد بيانات محفوظة' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json; charset=utf-8' }
        });
      }
    })());
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const response = await fetch(request);
          if (
            response &&
            response.ok &&
            response.status === 200 &&
            new URL(response.url).pathname === url.pathname
          ) {
            const cache = await caches.open(CACHE_NAME);
            await cache.put(request, response.clone());
          }
          return response;
        } catch (error) {
          const cached = await caches.match(request);
          if (cached) return cached;

          if (url.pathname === '/') {
            for (const path of ['/index', '/student/dashboard']) {
              const cachedHome = await caches.match(new URL(path, url.origin).href);
              if (cachedHome) return cachedHome;
            }
          }

          const offlineResponse = await caches.match(OFFLINE_FALLBACK_PATH);
          if (offlineResponse) return offlineResponse;

          return new Response('Offline', {
            status: 503,
            headers: { 'Content-Type': 'text/plain; charset=utf-8' }
          });
        }
      })()
    );
    return;
  }

  event.respondWith(
    (async () => {
      const cached = await caches.match(request);
      if (cached) return cached;

      try {
        const response = await fetch(request);
        if (response && response.ok && response.status === 200) {
          const cache = await caches.open(CACHE_NAME);
          await cache.put(request, response.clone());
        }
        return response;
      } catch (error) {
        return new Response('', { status: 503 });
      }
    })()
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') {
    self.skipWaiting();
  }
});

self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-attendance') {
    event.waitUntil(
      self.clients
        .matchAll({ type: 'window', includeUncontrolled: true })
        .then((clients) => {
          clients.forEach((client) => client.postMessage({ type: 'sync-attendance' }));
        })
    );
  }
});

console.log(`[SW] ${CACHE_NAME} loaded`);
