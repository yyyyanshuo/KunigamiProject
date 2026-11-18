// 管理员模拟登录状态条：只在服务器确认当前 Session 正在模拟用户时显示。
(function initAdminImpersonationBanner() {
    const t = (key, values) => window.SakiI18n ? window.SakiI18n.t(key,values) : key.replace(/\{(\w+)\}/g,(_,name)=>String((values||{})[name] ?? '{'+name+'}'));
    fetch('/api/admin/impersonation/status', { credentials: 'same-origin' })
        .then(function(response) {
            if (response.status === 440) {
                window.location.href = '/admin/dashboard';
                return null;
            }
            return response.ok ? response.json() : null;
        })
        .then(function(data) {
            if (!data || !data.active || document.getElementById('admin-impersonation-banner')) return;
            var banner = document.createElement('div');
            banner.id = 'admin-impersonation-banner';
            banner.setAttribute('role', 'alert');
            banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;gap:12px;padding:9px 12px;background:#b42318;color:#fff;font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.25);';
            var text = document.createElement('span');
            text.textContent = t('管理员模拟登录中：用户 {0}（15 分钟后自动退出）', {0:data.user_id});
            var button = document.createElement('button');
            button.type = 'button';
            button.textContent = t('退出模拟');
            button.style.cssText = 'border:1px solid rgba(255,255,255,.8);border-radius:6px;padding:4px 10px;background:#fff;color:#b42318;font-weight:600;cursor:pointer;';
            button.addEventListener('click', function() {
                button.disabled = true;
                fetch('/api/admin/impersonation/exit', { method: 'POST', credentials: 'same-origin' })
                    .then(function(response) { return response.json().then(function(body) { return { ok: response.ok, body: body }; }); })
                    .then(function(result) {
                        if (!result.ok) throw new Error(result.body.message || t('退出失败'));
                        window.location.href = result.body.redirect || '/admin/dashboard';
                    })
                    .catch(function(error) {
                        alert(error.message || t('退出模拟失败'));
                        button.disabled = false;
                    });
            });
            banner.appendChild(text);
            banner.appendChild(button);
            document.body.appendChild(banner);
        })
        .catch(function() {});
})();
