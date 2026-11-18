// Run against an isolated offline server with a synthetic admin account (ID 1).
const assert = require('node:assert/strict');
const { chromium } = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const base = process.env.SAKI_BROWSER_URL || 'http://127.0.0.1:5099';

(async () => {
    const browser = await chromium.launch({ headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
    try {
        const context = await browser.newContext({ baseURL: base, viewport: { width: 390, height: 844 }, serviceWorkers: 'block' });
        await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
        await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        assert.equal((await context.request.post('/api/login', { data: { username: 'test@saki.invalid', password: 'synthetic-browser-password' } })).status(), 200);
        // Account management must remain available even when statistics are unavailable.
        await page.route('**/api/admin/stats', route => route.fulfill({ status: 500, json: { error: 'synthetic stats failure' } }));
        await page.route('**/api/admin/circuit_status', route => route.fulfill({ json: { users: [] } }));
        for (const language of ['zh-CN', 'en', 'ja']) {
            await context.request.post('/api/ui/language', { data: { user_language: language } });
            assert.equal((await page.goto('/admin/dashboard')).status(), 200);
            await page.locator('#create-account-email').fill(`browser-${language}-${Date.now()}@saki.invalid`);
            await page.locator('#create-account-name').fill('<b>Synthetic user</b>');
            if (language === 'en') assert(!/[\u4e00-\u9fff]/.test(await page.locator('section.table-card').innerText()));
            const [response] = await Promise.all([
                page.waitForResponse(r => r.url().endsWith('/api/admin/users') && r.request().method() === 'POST'),
                page.locator('#create-account-btn').click(),
            ]);
            assert.equal(response.status(), 201);
            const account = await response.json();
            await page.locator('#create-account-result').waitFor({ state: 'visible' });
            const credentials = await page.locator('#create-account-credentials').inputValue();
            assert(credentials.includes(account.email) && credentials.includes(account.initial_password));
            assert(await page.locator('#create-account-btn').isDisabled());
            await page.locator('#copy-account-btn').click();
            await page.waitForFunction(text => navigator.clipboard.readText().then(value => value === text), credentials);
            const user = await browser.newContext({ baseURL: base });
            assert.equal((await user.request.post('/api/login', { data: { username: account.email, password: account.initial_password } })).status(), 200);
            assert.equal((await user.request.post('/api/admin/users', { data: { email: 'forbidden@saki.invalid' }, headers: { 'X-Requested-With': 'XMLHttpRequest' } })).status(), 403);
            await user.close();
            const box = await page.locator('section.table-card').boundingBox();
            assert(box.x >= 0 && box.x + box.width <= 391);
            await page.locator('#next-account-btn').click();
            assert.equal(await page.locator('#create-account-credentials').inputValue(), '');
            assert(await page.locator('#create-account-result').isHidden());
            assert(await page.locator('#create-account-btn').isEnabled());
            await page.locator('#create-account-email').fill(account.email);
            await page.locator('#create-account-btn').click();
            await page.waitForFunction(() => document.getElementById('create-account-status').textContent.length > 0);
            assert(await page.locator('#create-account-result').isHidden());
            assert(await page.locator('#create-account-btn').isEnabled());
        }
        assert.equal((await context.request.get('/register')).status(), 404);
        assert.equal((await context.request.post('/api/register', { data: { email: 'blocked@saki.invalid', password: 'unused-password' } })).status(), 404);
        assert.deepEqual(errors, []);
        console.log('PASS: account creation, copying, login, permissions, duplicate handling, three languages and mobile form');
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
