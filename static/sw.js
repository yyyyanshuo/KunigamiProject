self.addEventListener('install', (e) => {
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
    // 网络请求失败时处理
    e.respondWith(
        fetch(e.request)
            .catch(() => {
                // 网络不可用时，返回离线提示或缓存的响应
                return new Response('网络离线，请检查连接', {
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