self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
    // 什么都不做，直接透传请求，保证内容永远最新
    e.respondWith(fetch(e.request));
});