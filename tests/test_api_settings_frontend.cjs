// Run with Node + Playwright; optionally set KUNIGAMI_BROWSER_EXECUTABLE.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const source = fs.readFileSync(path.join(__dirname, '../templates/profile.html'), 'utf8');
const markup = source.slice(source.indexOf('<!-- API 线路与模型设置 -->'), source.indexOf('<!-- API 消耗日志面板 -->'));
const script = source.slice(source.indexOf('  let apiConfig = {};'), source.indexOf('  // 启动加载\n  loadApiConfig();'));

(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
    try {
        const page = await browser.newPage();
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('dialog', dialog => dialog.accept());
        let saved = {
            active_route: 'relay',
            routes: {
                relay: {relay_provider: 'new', models: {chat: 'old', summary: 'old'}},
                gemini: {models: {chat: 'gemini-old', summary: 'gemini-old'}}
            },
            model_options: {relay: ['old'], gemini: ['gemini-old']}
        };
        let posts = 0, fail = false;
        await page.route('http://settings.test/**', route => {
            if (route.request().url().endsWith('/api/system_config')) {
                if (route.request().method() === 'POST') {
                    posts++;
                    if (fail) return route.fulfill({status: 500, json: {error: 'failed'}});
                    saved = route.request().postDataJSON();
                    return route.fulfill({json: {status: 'success'}});
                }
                return route.fulfill({json: saved});
            }
            return route.fulfill({contentType: 'text/html; charset=utf-8', body: '<meta charset="utf-8">' + markup + '<script>const fetchWithAuthGuard = fetch; const handleApiUnauthorized = () => false;\n' + script + '</script>'});
        });
        await page.goto('http://settings.test/');
        await page.evaluate(async () => {await loadApiConfig(); toggleApiEdit(true);});
        const add = async name => {
            await page.locator('#new-model-input').fill(name);
            await page.getByRole('button', {name: '+ 添加', exact: true}).click();
        };
        await add('new-model');
        await page.locator('#model-chat').selectOption('new-model');
        await page.locator('#model-summary').selectOption('new-model');
        await page.locator('#relay_custom').check();
        await page.locator('#relay_custom_url').fill('https://relay.example/v1');
        // Another addition and deletion must preserve pending assignments and URL.
        await add('temporary');
        await page.evaluate(() => deleteModel('temporary'));
        assert.equal(await page.locator('#model-chat').inputValue(), 'new-model');
        assert.equal(await page.locator('#relay_custom_url').inputValue(), 'https://relay.example/v1');
        await page.locator('#radio-gemini').check();
        await add('gemini-new');
        await page.locator('#model-chat').selectOption('gemini-new');
        await page.locator('#radio-relay').check();
        assert.equal(await page.locator('#model-summary').inputValue(), 'new-model');
        await page.evaluate(() => saveApiConfig());
        assert.equal(posts, 1);
        assert.equal(saved.routes.relay.models.chat, 'new-model');
        assert.equal(saved.routes.relay.models.summary, 'new-model');
        assert.equal(saved.routes.gemini.models.chat, 'gemini-new');
        assert.equal(saved.routes.relay.relay_custom_url, 'https://relay.example/v1');
        assert(saved.model_options.relay.includes('new-model'));
        assert(!saved.model_options.relay.includes('temporary'));
        await page.evaluate(async () => {await loadApiConfig(); toggleApiEdit(true);});
        assert.equal(await page.locator('#model-chat').inputValue(), 'new-model');
        // Cancel discards both list edits and assignments.
        await add('cancelled');
        await page.locator('#model-chat').selectOption('cancelled');
        await page.evaluate(() => {toggleApiEdit(false); toggleApiEdit(true);});
        assert.equal(await page.locator('#model-chat').inputValue(), 'new-model');
        assert.equal(await page.locator('#model-chat option[value="cancelled"]').count(), 0);
        // A failed save retains the draft, without updating the saved view.
        await add('retry-model');
        await page.locator('#model-chat').selectOption('retry-model');
        fail = true;
        await page.evaluate(() => saveApiConfig());
        assert.equal(await page.locator('#model-chat').inputValue(), 'retry-model');
        assert.equal(await page.locator('#view-chat-model').textContent(), 'new-model');
        fail = false;
        await page.evaluate(() => saveApiConfig());
        assert.equal(saved.routes.relay.models.chat, 'retry-model');
        // Empty custom URLs must stop cleanly before posting.
        await page.evaluate(() => toggleApiEdit(true));
        await page.locator('#relay_custom_url').fill('');
        const before = posts;
        await page.evaluate(() => saveApiConfig());
        assert.equal(posts, before);
        assert.equal(await page.locator('#module-api .btn-save').isEnabled(), true);
        assert.deepEqual(errors, []);
        console.log('PASS: one-save model updates, route drafts, cancel, retry and custom URL validation');
    } finally {
        await browser.close();
    }
})().catch(error => {console.error(error); process.exitCode = 1;});
