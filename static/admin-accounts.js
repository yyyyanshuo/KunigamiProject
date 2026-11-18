(() => {
    const form = document.getElementById('create-account-form');
    const email = document.getElementById('create-account-email');
    const name = document.getElementById('create-account-name');
    const submit = document.getElementById('create-account-btn');
    const status = document.getElementById('create-account-status');
    const result = document.getElementById('create-account-result');
    const credentials = document.getElementById('create-account-credentials');
    let busy = false;

    function reset() {
        credentials.value = '';
        result.hidden = true;
        status.textContent = '';
        form.reset();
        email.disabled = name.disabled = submit.disabled = false;
    }

    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || !result.hidden || !form.reportValidity()) return;
        busy = true;
        submit.disabled = true;
        status.textContent = '';
        submit.textContent = SakiI18n.t('正在创建...');
        try {
            const response = await fetch('/api/admin/users', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                body: JSON.stringify({email: email.value.trim(), display_name: name.value.trim()})
            });
            const data = await response.json();
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.message || data.error || SakiI18n.t('创建账号失败，请稍后重试'));
            }
            credentials.value = [
                SakiI18n.t('登录地址：{url}', {url: new URL('/login', window.location.origin).href}),
                SakiI18n.t('账号邮箱：{email}', {email: data.email}),
                SakiI18n.t('初始密码：{password}', {password: data.initial_password}),
                SakiI18n.t('请在登录后修改密码。')
            ].join('\n');
            result.hidden = false;
            email.disabled = name.disabled = true;
            credentials.focus();
        } catch (error) {
            status.textContent = error.message || SakiI18n.t('网络错误');
        } finally {
            busy = false;
            submit.disabled = !result.hidden;
            submit.textContent = SakiI18n.t('创建账号');
        }
    });

    document.getElementById('copy-account-btn').addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(credentials.value);
            status.textContent = SakiI18n.t('已复制登录信息');
        } catch (_) {
            credentials.focus();
            credentials.select();
            status.textContent = SakiI18n.t('无法自动复制，请手动复制已选中的登录信息');
        }
    });
    document.getElementById('next-account-btn').addEventListener('click', () => {
        reset();
        email.focus();
    });
    // Clear secrets before the document can enter the browser's back/forward cache.
    window.addEventListener('pagehide', reset);
})();
