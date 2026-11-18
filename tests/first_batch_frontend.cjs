const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.SAKI_PLAYWRIGHT_MODULE || 'C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.join(__dirname, '..');
const groupSource = fs.readFileSync(path.join(root, 'templates/group_memory.html'), 'utf8');
const memorySource = fs.readFileSync(path.join(root, 'templates/memory.html'), 'utf8');
const calendarFunctions = memorySource.slice(memorySource.indexOf('    const MEMORY_TIMEZONE ='), memorySource.indexOf('    function showToast')).replace(/{{ memory_timezone \| default\('Asia\/Shanghai'\) \| tojson }}/g, '"Asia/Shanghai"');
const profile = fs.readFileSync(path.join(root, 'templates/profile.html'), 'utf8');
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'static/locales/ui.json'), 'utf8'));
const ui = fs.readFileSync(path.join(root, 'static/ui-language.js'), 'utf8');
const groupFunctions = groupSource.slice(groupSource.indexOf('    function renderMemoryView()'), groupSource.indexOf('    // 群名称失焦时即时保存')).replace(/{{ memory_timezone \| default\('Asia\/Shanghai'\) \| tojson }}/g, '"Asia/Shanghai"');
const groupActions = groupSource.slice(groupSource.indexOf('            <div id="edit-actions"'), groupSource.indexOf('    <!-- 危险区域 -->'));
const switchFunction = profile.slice(profile.indexOf('  let accountSwitchPending ='), profile.indexOf('  // --- 账号注销 ---'));
function i18n(language) {
    const translated = Object.fromEntries(Object.entries(catalog).map(([key, values]) => [key, values[language]]));
    return `<script>window.SakiUICatalog=${JSON.stringify(translated)};</script><script>${ui}</script>`;
}
(async () => {
    const browser = await chromium.launch({headless: true,
        executablePath: process.env.SAKI_BROWSER_EXECUTABLE || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
        args: ['--disable-background-networking', '--disable-sync', '--no-first-run']});
    try {
        for (const language of ['zh-CN', 'en', 'ja']) {
            const context = await browser.newContext({viewport: {width:390, height:844}, timezoneId:'America/Los_Angeles', serviceWorkers:'block'});
            let stored = {}, saves = 0;
            await context.route('**/*', route => {
                const url = new URL(route.request().url());
                if (url.hostname !== 'group.test') return route.abort();
                if (url.pathname.endsWith('/save_memory')) {
                    stored = route.request().postDataJSON().content;
                    saves++;
                    return route.fulfill({json:{status:'success'}});
                }
                return route.fulfill({contentType:'text/html', body:`<html lang="${language}"><head><meta charset="utf-8">${i18n(language)}</head><body>
                    <button id="editToggleBtn" onclick="toggleEditMode()">Edit</button>
                    <div id="memory-container"></div>${groupActions}
                    <script>let globalData={short:${JSON.stringify(stored)}};let isEditing=false;const groupId='test';${groupFunctions};renderMemoryView();</script></body></html>`});
            });
            const page = await context.newPage(), errors = [];
            page.on('pageerror', error => errors.push(error.message));
            page.on('dialog', dialog => dialog.accept());
            await page.clock.install({time:new Date('2026-10-27T17:30:00Z')});
            await page.goto('http://group.test/memory');
            await page.addScriptTag({content:calendarFunctions});
            assert.equal(await page.evaluate(() => beijingDateString()), '2026-10-28');
            assert.equal(await page.evaluate(() => beijingHour()), 1);
            assert.equal(await page.evaluate(() => shiftBeijingDate('2028-03-01', -1)), '2028-02-29');
            assert.equal(await page.locator('#edit-actions').isVisible(), false);
            await page.locator('#editToggleBtn').click();
            assert.equal(await page.locator('#memory-entry-date').inputValue(), '2026-10-28');
            await page.locator('[onclick="addMemoryRow()"]').click();
            const eventText = '原文 "quotes" <b>不要变成标签</b> & 内容';
            await page.locator('.input-key').fill('10:00');
            await page.locator('.input-val').fill(eventText);
            await page.locator('[onclick="addMemoryRow()"]').click();
            assert.equal(await page.locator('.day-block').count(), 1);
            assert.equal(await page.locator('.input-val').first().inputValue(), eventText);
            await page.locator('.input-key').last().fill('11:30');
            await page.locator('.input-val').last().fill('第二条');
            await page.locator('#memory-entry-date').fill('2026-10-27');
            await page.locator('[onclick="addMemoryRow()"]').click();
            await page.locator('[data-date="2026-10-27"] .input-key').fill('23:10');
            await page.locator('[data-date="2026-10-27"] .input-val').fill('前一天');
            assert.deepEqual(await page.locator('.day-block').evaluateAll(nodes => nodes.map(node => node.dataset.date)), ['2026-10-28','2026-10-27']);
            await page.locator('[onclick="saveMemory()"]').click();
            await page.waitForFunction(() => !isEditing);
            assert.equal(saves, 1);
            assert.equal(stored['2026-10-28'].events[0].event, eventText);
            assert.equal(stored['2026-10-28'].events.length, 2);
            await page.reload();
            assert.equal(await page.locator('[data-model-output]').first().textContent(), eventText);
            assert.equal(await page.locator('#memory-container b').count(), 0);
            stored['2026-10-28'].last_id = 42;
            await page.reload();
            await page.locator('#editToggleBtn').click();
            await page.locator('[data-date="2026-10-28"] [onclick="addEventRow(this)"]').click();
            const day = page.locator('[data-date="2026-10-28"]');
            await day.locator('.input-key').last().fill('12:00');
            await day.locator('.input-val').last().fill('第三条');
            await page.locator('[onclick="saveMemory()"]').click();
            await page.waitForFunction(() => !isEditing);
            assert.equal(stored['2026-10-28'].last_id, 42);
            await page.locator('#editToggleBtn').click();
            await day.locator('.input-val').first().fill('不保存的修改');
            await page.locator('#editToggleBtn').click();
            assert.equal(await page.locator('[data-model-output]').first().textContent(), eventText);
            assert.deepEqual(errors, []);
            await context.close();
        }
        const context = await browser.newContext({serviceWorkers:'block'});
        let requests = 0, release, success = false;
        const pending = new Promise(resolve => { release = resolve; });
        await context.route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.hostname !== 'switch.test') return route.abort();
            if (url.pathname === '/api/accounts/switch') {
                requests++;
                if (!success) {
                    await pending;
                    return route.fulfill({status:503,json:{status:'error', message:'Try again'}});
                }
                return route.fulfill({json:{status:'success',user_id:2}});
            }
            if (url.pathname === '/') return route.fulfill({contentType:'text/html',body:'Switched'});
            return route.fulfill({contentType:'text/html', body:`<html lang="en"><head><meta charset="utf-8">${i18n('en')}</head><body>
                <div id="account-switch-list"><button onclick="switchAccount(2)">Switch</button></div>
                <script>const fetchWithAuthGuard=(...args)=>fetch(...args);${switchFunction}</script></body></html>`});
        });
        const page = await context.newPage(), errors = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('dialog', dialog => dialog.accept());
        await page.goto('http://switch.test/profile');
        await page.locator('button').click();
        await page.evaluate(() => switchAccount(2));
        assert.equal(requests, 1);
        assert.equal(await page.locator('button').isDisabled(), true);
        release();
        await page.waitForFunction(() => !accountSwitchPending);
        assert.equal(await page.locator('button').isEnabled(), true);
        success = true;
        await page.locator('button').click();
        await page.waitForURL('http://switch.test/');
        assert.equal(requests, 2);
        assert.deepEqual(errors, []);
        await context.close();
        console.log('PASS: group memory add/save/reload/cancel in three UI languages; account switch duplicate-click guard and failure retry');
    } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
