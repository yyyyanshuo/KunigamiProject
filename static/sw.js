self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
    // 保持透传，不缓存
    e.respondWith(fetch(e.request));
});

// --- 【新增】监听推送事件 ---
self.addEventListener('push', function(event) {
    if (event.data) {
        const data = event.data.json();
        const title = data.title || '新消息';
        const options = {
            body: data.body || '你收到了一条消息',
            icon: '/static/logo.png', // 图标
            badge: '/static/logo.png', // 安卓状态栏小图标
            data: { url: data.url || '/' } // 点击跳转地址
        };

        event.waitUntil(
            self.registration.showNotification(title, options)
        );
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