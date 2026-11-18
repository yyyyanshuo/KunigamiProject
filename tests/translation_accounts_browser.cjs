const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.join(__dirname, '..');
const privacy = fs.readFileSync(path.join(root, 'static/privacy-session.js'), 'utf8');
const template = fs.readFileSync(path.join(root, 'templates/chat.html'), 'utf8');
const functions = template.slice(template.indexOf('  const CHAT_TRANS_CACHE_PREFIX ='), template.indexOf('  // --- 【新增】解决“返回不刷新”的问题 ---'));

function html(owner, group, language = 'zh-CN') {
    return `<html lang="${language}"><head><script>
        window.SakiPrivacyUser = ${JSON.stringify(owner)};
        window.SakiI18n = {t: text => text};
        ${privacy}
    </script></head><body><a href="/logout">Logout</a><div id="chatContainer">
        <div class="message-group user" data-id="42" data-original-content="こんにちは">
            <div class="message-wrapper"><button>Translate</button></div>
            <div class="message-wrapper"><button>Translate</button></div>
        </div>
        <div class="message-group assistant" data-id="43" data-original-content="こんばんは">
            <div class="message-wrapper"><button>Translate</button></div>
        </div></div><script>
        const chatContainer = document.getElementById('chatContainer');
        const pendingSentTranslations = new Map();
        const currentId = 'demo', aiLanguage = 'ja', isGroupMode = ${group};
        const currentConfig = {}, groupMembers = {};
        function stripRubyTags(text) { return text; }
        ${functions}
        document.querySelectorAll('button').forEach(button => {
            button.onclick = () => translateMessage(button.parentNode, button.closest('.message-group').dataset.id, button.closest('.message-group'));
        });
        showCachedTranslations();
    </script></body></html>`;
}

(async () => {
    const browser = await chromium.launch({headless: true,
        executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
        args: ['--disable-background-networking', '--disable-sync', '--no-first-run']});
    try {
        for (const group of [false, true]) {
            const context = await browser.newContext({serviceWorkers: 'block'});
            const requests = [], errors = [];
            await context.route('**/*', route => {
                const url = new URL(route.request().url());
                if (url.hostname !== 'translation.test') return route.abort();
                if (url.pathname === '/api/translate') {
                    requests.push(route.request().postDataJSON());
                    return route.fulfill({json: {result: '重新翻译的正文'}});
                }
                const owner = /^\/user\/([12])$/.exec(url.pathname)?.[1] || null;
                return route.fulfill({contentType: 'text/html', body: html(owner, group, url.searchParams.get('lang') || 'zh-CN')});
            });
            const page = await context.newPage();
            page.on('pageerror', error => errors.push(error.message));
            const load = (owner, language = 'zh-CN') => page.goto(`http://translation.test/user/${owner}?lang=${language}`);
            const userText = page.locator('[data-id="42"] .translation-source, [data-id="42"] .translation-result');
            const aiText = page.locator('[data-id="43"] .translation-result');
            await load(1);
            await page.evaluate(group => {
                const type = group ? 'g' : 'c';
                const result = `kt_chat_v2_zh-CN_${type}_demo_43`;
                const source = `kt_chat_source_users/1_${type}_demo_42`;
                localStorage.setItem(result, '账号一的旧译文');
                localStorage.setItem(source, '账号一的原句');
                localStorage.setItem('kt_chat_hidden', JSON.stringify([type + '|demo|43']));
                localStorage.setItem('kt_chat_trans_lru', JSON.stringify([result, source]));
            }, group);
            // The first page after upgrading may belong to another account.
            await load(2);
            assert.equal(await page.locator('.translation-source, .translation-result').count(), 0);
            await load(1);
            assert.equal(await userText.innerText(), '账号一的原句');
            assert.equal(await aiText.count(), 0);
            assert.equal(await page.evaluate(() => _chatTransGet(_getChatTransCacheKey(43))), '账号一的旧译文');
            assert.deepEqual(await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('kt_chat_'))), []);
            await page.evaluate(() => {
                sessionStorage.setItem('forumFormState', 'private draft');
                localStorage.setItem('unscoped-private-cache', 'private');
            });
            await load(2);
            assert.equal(await userText.count(), 0);
            assert.equal(await aiText.count(), 0);
            assert.equal(await page.evaluate(() => sessionStorage.getItem('forumFormState')), null);
            assert.equal(await page.evaluate(() => localStorage.getItem('unscoped-private-cache')), null);
            await page.evaluate(() => {
                _chatTransSet(_getChatSourceCacheKey(42), '账号二的原句');
                _chatTransSet(_getChatTransCacheKey(43), '账号二的译文');
                showCachedTranslations();
            });
            assert.equal(await userText.innerText(), '账号二的原句');
            assert.equal(await aiText.innerText(), '账号二的译文');
            await load(1);
            assert.equal(await userText.innerText(), '账号一的原句');
            assert.equal(await aiText.count(), 0);

            // Hidden state survives switching; showing it again requests a fresh
            // translation and retires the original source, including after reload.
            await page.locator('[data-id="42"] button').last().click();
            assert(await userText.isHidden());
            await load(2);
            assert.equal(await userText.innerText(), '账号二的原句');
            await load(1);
            assert.equal(await userText.count(), 0);
            await page.locator('[data-id="42"] button').first().click();
            await page.waitForFunction(() => document.querySelector('[data-id="42"] .translation-result')?.textContent === '重新翻译的正文');
            assert.equal(requests.length, 1);
            assert.equal(requests[0].message_id, 42);
            assert.equal(requests[0][group ? 'group_id' : 'char_id'], 'demo');
            assert.equal(await page.locator('.translation-source').count(), 0);
            await load(1);
            assert.equal(await userText.innerText(), '重新翻译的正文');
            await load(1, 'en');
            assert.equal(await userText.count(), 0);
            await load(1);
            assert.equal(await userText.innerText(), '重新翻译的正文');

            await page.locator('a[href="/logout"]').click();
            await page.waitForURL('**/logout');
            assert.equal(await page.locator('.translation-source, .translation-result').count(), 0);
            await load(1);
            assert.equal(await userText.innerText(), '重新翻译的正文');
            await load(2);
            assert.equal(await userText.innerText(), '账号二的原句');
            // Explicit privacy cleanup/account deletion keeps the existing full
            // site cleanup behavior, unlike switching or ordinary logout.
            await page.evaluate(() => SakiPrivacy.clear());
            await load(2);
            assert.equal(await page.locator('.translation-source, .translation-result').count(), 0);
            await load(1);
            assert.equal(await userText.count(), 0);

            // Account-local eviction cannot follow a stale/corrupt index into
            // another account, and the 800-entry bound actually removes values.
            const eviction = await page.evaluate(() => {
                const foreign = 'saki:translations:2:result_zh-CN_c_demo_99';
                localStorage.setItem(foreign, 'keep other account');
                localStorage.setItem(CHAT_TRANS_LRU_KEY, JSON.stringify([foreign]));
                for (let id = 1000; id <= 1800; id++) _chatTransSet(_getChatTransCacheKey(id), 'cache');
                return {foreign: localStorage.getItem(foreign), first: _chatTransGet(_getChatTransCacheKey(1000)),
                    last: _chatTransGet(_getChatTransCacheKey(1800)), length: _chatTransLruLoad().length};
            });
            assert.deepEqual(eviction, {foreign: 'keep other account', first: null, last: 'cache', length: 800});
            assert.deepEqual(errors, []);
            await context.close();
        }
        console.log('PASS: single/group migration, account isolation and restoration, hide/retranslate, languages, logout, explicit privacy cleanup and LRU eviction');
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
