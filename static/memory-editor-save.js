// Manual edits compare only the entries the user changed. Keep drafts in this tab.
(() => {
    const keys = ['short', 'medium', 'long'];
    const unconfirmed = new WeakMap();
    const t = (text) => SakiI18n.t(text);

    function eventRows(day) {
        const events = Array.isArray(day) ? day : (day?.events || []);
        return events.map(item => ({time: item.time || '', event: item.event || ''}));
    }

    function collect(key, editor, original) {
        const changes = Object.create(null);
        if (key === 'short') {
            const period = editor.dataset.dateKey;
            const before = eventRows(original[period]);
            const after = Array.from(editor.querySelectorAll('.short-edit-row'), row => ({
                time: row.querySelector('.input-key').value.trim(),
                event: row.querySelector('.input-val').value
            })).filter(row => row.time || row.event);
            const reset = editor.querySelector('#reset-progress-chk').checked;
            if (reset || JSON.stringify(before) !== JSON.stringify(after)) {
                changes[period] = {before, after, reset};
                if (reset) {
                    const day = original[period] || {};
                    changes[period].progress = [day.last_id || 0, day.call_last_id || 0, day.coverage || [], day.call_coverage || []];
                }
            }
        } else {
            const entries = new Map();
            for (const row of editor.querySelectorAll('.input-group')) {
                const period = row.querySelector('.input-key').value.trim();
                const value = row.querySelector('.input-val').value;
                if (!period && !value.trim()) continue;
                if (!period || entries.has(period)) throw new Error(t('请为每条记忆填写不同的日期或周期。'));
                entries.set(period, value);
            }
            for (const period of new Set([...Object.keys(original), ...entries.keys()])) {
                const before = Object.prototype.hasOwnProperty.call(original, period) ? original[period] : null;
                const after = entries.has(period) ? entries.get(period) : null;
                if (before !== after) changes[period] = {before, after};
            }
        }
        return changes;
    }

    function message(status, code) {
        if (status === 401) return t('登录已过期。请在新标签页登录同一账号，再回来保存。');
        if (status === 403 || status === 404) return t('当前账号无法访问这个角色。请先核对账号和角色，编辑内容仍在此页。');
        if (status === 409) return t('同一条记忆已在别处更新。请复制本页修改，再刷新核对。');
        if (status === 413) return t('本次修改内容过大，请缩短单条记忆后重试。');
        if (code === 'memory_busy') return t('记忆正在更新，请稍后再点保存。编辑内容仍在此页。');
        if (code === 'storage_write_failed') return t('服务器暂时无法保存，请稍后重试。');
        if (status === 400) return t('记忆格式无效，请检查日期及内容后重试。');
        return t('未收到保存确认。编辑内容仍在此页，可以重试。') + (status ? ' (HTTP ' + status + ')' : '');
    }

    async function save(btn, module, key, original, owner, url, render) {
        if (btn.disabled) return;
        const editor = module.querySelector('.edit-mode');
        const cancel = module.querySelector('.btn-cancel');
        const notice = module.querySelector('.memory-save-notice');
        const label = btn.textContent;
        btn.disabled = cancel.disabled = editor.inert = true;
        btn.textContent = t('保存中...');
        notice.textContent = '';
        try {
            const changes = collect(key, editor, original);
            // Also verify entries the user reverted locally after losing a response.
            // The server may have committed the previous attempt.
            for (const [period, prior] of Object.entries(unconfirmed.get(module) || {})) {
                if (!(period in changes)) changes[period] = {before: prior.before, after: prior.before};
            }
            if (Object.keys(changes).length) {
                let response, body;
                unconfirmed.set(module, changes);
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 30000);
                try {
                    response = await fetch(url, {method: 'POST', signal: controller.signal,
                        headers: {'Content-Type':'application/json'}, body: JSON.stringify({key, changes, owner})});
                    body = await response.json().catch(() => null);
                } catch {
                    throw new Error(message(0));
                } finally {
                    clearTimeout(timer);
                }
                if (!response.ok || body?.status !== 'success' || !body.applied) {
                    const status = response.redirected && new URL(response.url).pathname === '/login' ? 401 : response.status;
                    throw new Error(message(status, body?.code));
                }
                for (const period of Object.keys(changes)) {
                    if (!Object.prototype.hasOwnProperty.call(body.applied, period)) throw new Error(message(response.status));
                }
                for (const [period, value] of Object.entries(body.applied)) {
                    if (value === null) delete original[period];
                    else Object.defineProperty(original, period, {value, writable:true, enumerable:true, configurable:true});
                }
            }
            unconfirmed.delete(module);
            module.classList.remove('editing');
            try {
                render(key, original);
                notice.textContent = t('✓ 已保存');
            } catch {
                notice.textContent = t('保存已确认。请刷新页面查看最新显示。');
            }
        } catch (error) {
            notice.textContent = error.message;
        } finally {
            btn.disabled = cancel.disabled = editor.inert = false;
            btn.textContent = label;
        }
    }

    window.SakiMemoryEditor = {handles: key => keys.includes(key), save};
})();
