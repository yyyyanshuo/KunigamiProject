importScripts('/static/locales/worker.js');
async function workerLanguage() {
    const cache = await caches.open('saki-ui-language');
    const value = await cache.match('/__saki_ui_language__');
    return value ? value.text() : 'ja';
}
function workerText(key, language) {
    return self.SakiWorkerMessages[key][language] || self.SakiWorkerMessages[key].ja;
}
self.addEventListener('message', event => {
    if (event.data?.type === 'saki-ui-language' && ['ja','en','zh-CN'].includes(event.data.language)) {
        event.waitUntil(caches.open('saki-ui-language').then(cache => cache.put('/__saki_ui_language__', new Response(event.data.language))));
    }
});
self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
    // Let the browser own recording uploads: proxying them through fetch hides
    // XHR upload progress and disconnects cancellation from the active request.
    const url = new URL(e.request.url);
    if (e.request.method === 'POST' && url.origin === self.location.origin &&
        /^\/api\/calls\/[^/]+\/transcribe$/.test(url.pathname)) return;
    // 网络请求失败时处理
    e.respondWith(
        fetch(e.request)
            .catch(async () => {
                // 网络不可用时，返回离线提示或缓存的响应
                return new Response(workerText('网络离线，请检查连接', await workerLanguage()), {
                    status: 503,
                    statusText: 'Service Unavailable',
                    headers: new Headers({
                        'Content-Type': 'text/plain'
                    })
                });
            })
    );
});

// --- 【新增】监听推送事件 ---
self.addEventListener('push', function(event) {
    if (event.data) {
        const data = event.data.json();
        event.waitUntil((async () => {
            const language = data.language || await workerLanguage();
            await self.registration.showNotification(data.title || workerText('新消息',language), {
                body: data.body || workerText('你收到了一条消息',language),
                icon:'/static/logo.png', badge:'/static/logo.png', data:{url:data.url || '/'}
            });
        })());
    }
});

// --- 【新增】监听通知点击事件 ---
self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    // 点击通知打开网页
    event.waitUntil(
        clients.openWindow(event.notification.data.url)
    );
});
