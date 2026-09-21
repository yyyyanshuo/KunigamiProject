/* Durable retries contain only conversation IDs and observed message IDs. */
(function () {
    const memory = new Map();
    const prefix = userId => `kunigami_pending_read:${userId}:`;
    const keyFor = (userId, item) => `${prefix(userId)}${item.type}:${encodeURIComponent(item.id)}`;

    function read(key) {
        try { return JSON.parse(localStorage.getItem(key)) || memory.get(key); }
        catch (_) { return memory.get(key); }
    }

    function queue(userId, item) {
        const key = keyFor(userId, item);
        const old = read(key);
        const value = {...item, last_message_id: Math.max(old?.last_message_id || 0, item.last_message_id)};
        memory.set(key, value);
        try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
    }

    function acknowledge(userId, item) {
        const key = keyFor(userId, item);
        const current = read(key);
        if (current && current.last_message_id <= item.last_message_id) {
            memory.delete(key);
            try { localStorage.removeItem(key); } catch (_) {}
        }
    }

    function pending(userId) {
        const keys = new Set(memory.keys());
        try {
            for (let i = 0; i < localStorage.length; i++) keys.add(localStorage.key(i));
        } catch (_) {}
        return Array.from(keys).filter(key => key?.startsWith(prefix(userId))).map(read).filter(Boolean);
    }

    async function post(url, body, keepalive = false) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        try {
            const res = await fetchWithAuthGuard(url, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body), keepalive, signal: controller.signal
            });
            if (handleApiUnauthorized(res)) throw new Error('登录已失效，请重新登录');
            const result = await res.json().catch(() => null);
            if (!res.ok) throw new Error(`已读状态保存失败（HTTP ${res.status}）：${result?.error || '请稍后重试'}`);
            if (result?.status !== 'success' || !Array.isArray(result.conversations)) {
                throw new Error('已读接口返回格式不正确，请同步新版后端文件并重启服务');
            }
            const requested = body.conversations || [body];
            for (const item of requested) {
                const confirmed = result.conversations.find(c => c.id === item.id && c.type === item.type);
                if (!confirmed || !Number.isSafeInteger(confirmed.last_read_id) || confirmed.last_read_id < item.last_message_id) {
                    throw new Error('服务器未确认已读进度，请刷新列表后重试');
                }
            }
            return result;
        } finally { clearTimeout(timeout); }
    }

    async function send(userId, item, keepalive = false) {
        queue(userId, item);
        await post(`/api/${encodeURIComponent(item.id)}/mark_read`, item, keepalive);
        acknowledge(userId, item);
    }

    async function flush(userId, contacts) {
        const allowed = new Set(contacts.map(c => `${c.type === 'group' ? 'group' : 'chat'}:${c.id}`));
        const items = pending(userId).filter(item => {
            if (allowed.has(`${item.type}:${item.id}`)) return true;
            acknowledge(userId, item); // A deleted conversation must not block other receipts.
            return false;
        });
        if (!items.length) return false;
        await saveBatch(userId, items);
        return true;
    }

    async function saveBatch(userId, conversations) {
        await post('/api/contacts/mark_read', {conversations});
        conversations.forEach(item => acknowledge(userId, item));
    }

    window.ReadReceipts = {queue, acknowledge, pending, send, flush, saveBatch};
})();
