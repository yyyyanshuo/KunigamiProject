// Run with Node + Playwright; KUNIGAMI_BROWSER_EXECUTABLE may select installed Chrome.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const source = name => fs.readFileSync(path.join(root, name), 'utf8');
const render = html => html.replace(/<!--[\s\S]*?-->/g, '').replace(/\{\{[\s\S]*?\}\}/g, '1').replace(/\{%[\s\S]*?%\}/g, '');

(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
    try {
        const page = await browser.newPage({viewport: {width: 375, height: 720}});
        const errors = [];
        const dialogs = [];
        page.on('pageerror', e => errors.push(e.message));
        page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
        let contacts = [
            {id: 'hero', type: 'group', remark: '这是一个非常非常长的群聊名称'.repeat(5), unread: 120, last_message_id: 120, last_time: '23:59', last_msg: '群聊消息'},
            {id: 'hero', type: 'chat', remark: 'VeryLongSingleChatName'.repeat(8), unread: 5, last_message_id: 5, last_time: '09:00', last_msg: '单聊消息'},
            ...Array.from({length: 18}, (_, i) => ({id: `friend${i}`, type: 'chat', remark: `联系人${i}`, unread: 0, last_message_id: 0, last_msg: 'hello'}))
        ];
        let gets = 0, failReads = false, addArrival = false, legacyContacts = false, legacyReadResponse = false;
        const batches = [], singles = [];
        const applyRead = item => {
            const c = contacts.find(c => c.id === item.id && c.type === item.type);
            if (c) c.unread = Math.max(0, c.last_message_id - item.last_message_id);
        };
        await page.route('**/*', async route => {
            const req = route.request(), url = new URL(req.url());
            if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: render(source('templates/contacts.html'))});
            if (url.pathname === '/chat-harness') return route.fulfill({contentType: 'text/html', body: '<div id="chatContainer" style="height:200px;overflow:auto"></div><script src="/static/auth-guard.js"></script><script src="/static/read-receipts.js"></script>'});
            if (['/static/auth-guard.js', '/static/read-receipts.js'].includes(url.pathname)) return route.fulfill({contentType: 'text/javascript', body: source(url.pathname.slice(1))});
            if (url.pathname === '/api/contacts') {
                gets++;
                return route.fulfill({json: legacyContacts ? contacts.map(({last_message_id, ...c}) => c) : contacts});
            }
            if (url.pathname === '/api/contacts/mark_read') {
                if (failReads) return route.fulfill({status: 500, json: {error: 'failed'}});
                const items = req.postDataJSON().conversations;
                if (legacyReadResponse) return route.fulfill({json: {status: 'success'}});
                batches.push(items);
                if (addArrival) { contacts[0].last_message_id++; addArrival = false; }
                items.forEach(applyRead);
                return route.fulfill({json: {status: 'success', conversations: items.map(c => ({type: c.type, id: c.id, last_read_id: c.last_message_id}))}});
            }
            if (url.pathname === '/api/hero/mark_read') {
                if (failReads) return route.fulfill({status: 500, json: {error: 'failed'}});
                const item = req.postDataJSON();
                singles.push(item);
                applyRead(item);
                return route.fulfill({json: {status: 'success', conversations: [{type: item.type, id: item.id, last_read_id: item.last_message_id}]}});
            }
            if (url.pathname.endsWith('.js')) return route.fulfill({contentType: 'text/javascript', body: ''});
            if (url.pathname.startsWith('/api/')) return route.fulfill({json: {enabled: true}});
            return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="46" height="46"><rect width="46" height="46" fill="#fee"/></svg>'});
        });
        await page.goto('http://kunigami.test/');
        await page.locator('#theme-loading-screen').evaluate(el => el.remove());
        await page.waitForFunction(() => document.querySelectorAll('.contact-item').length === 20);
        for (const width of [320, 375, 1024]) {
            await page.setViewportSize({width, height: 720});
            const dimensions = await page.locator('.contact-item').evaluateAll(rows => rows.slice(0, 2).map(row => {
                const badge = row.querySelector('.unread-badge').getBoundingClientRect();
                const name = row.querySelector('.name').getBoundingClientRect();
                const time = row.querySelector('.time').getBoundingClientRect();
                return {badgeLeft: badge.left, badgeRight: badge.right, nameRight: name.right, timeLeft: time.left, width: innerWidth};
            }));
            for (const d of dimensions) {
                assert(d.badgeRight <= d.width && d.badgeLeft >= d.nameRight && d.badgeRight <= d.timeLeft);
            }
            assert.equal(await page.locator('.unread-badge').first().innerText(), '99+');
        }
        await page.setViewportSize({width: 375, height: 720});
        if (process.env.KUNIGAMI_TEST_SCREENSHOT_DIR) {
            await page.screenshot({path: path.join(process.env.KUNIGAMI_TEST_SCREENSHOT_DIR, 'contacts-unread-mobile.png')});
        }
        // A partially updated deployment must never silently submit message ID zero.
        legacyContacts = true;
        await page.evaluate(() => loadContacts());
        await page.evaluate(() => markAllContactsRead());
        assert.match(dialogs.at(-1), /app\.py/);
        assert.equal(batches.length, 0);
        await page.evaluate(() => loadContacts());
        assert.match(await page.locator('#contactsFeedback').innerText(), /app\.py/);
        await page.locator('#btnMultiselect').click();
        await page.locator('.select-checkbox').first().check();
        await page.evaluate(() => runBatchAction('mark_read'));
        assert.equal(await page.evaluate(() => pendingOps.length), 0);
        assert.equal(batches.length, 0);
        legacyContacts = false;
        await page.evaluate(() => cancelSelectMode());
        await page.evaluate(() => contactsRequest);
        legacyReadResponse = true;
        await page.evaluate(() => markAllContactsRead());
        assert.match(dialogs.at(-1), /返回格式不正确/);
        assert.equal(batches.length, 0);
        legacyReadResponse = false;
        addArrival = true;
        await page.locator('#btnMarkAllRead').click();
        await page.waitForFunction(() => document.querySelector('.unread-badge')?.textContent === '1');
        assert.equal(batches[0][0].last_message_id, 120);
        assert.equal(await page.locator('.unread-badge').count(), 1);

        // Multiselect keeps checked nodes, draft operations, order and scroll on refresh.
        await page.locator('#btnMultiselect').click();
        await page.locator('.select-checkbox').first().check();
        await page.evaluate(() => { window.selectedNode = document.querySelector('.contact-item'); runBatchAction('mark_read'); });
        const beforeBatchCount = batches.length;
        contacts[0].unread = 2;
        contacts[0].last_message_id++;
        contacts = [contacts[1], contacts[0], ...contacts.slice(2)];
        await page.evaluate(() => window.scrollTo(0, 200));
        await page.evaluate(() => loadContacts());
        assert.equal(await page.evaluate(() => document.querySelector('.contact-item') === window.selectedNode), true);
        assert.equal(await page.locator('.select-checkbox').first().isChecked(), true);
        assert.equal(await page.evaluate(() => pendingOps.length), 1);
        assert.equal(await page.evaluate(() => scrollY), 200);
        assert.equal(batches.length, beforeBatchCount);
        await page.evaluate(() => saveSelectModeAndExit());
        assert.equal(batches.at(-1)[0].last_message_id, 121); // Snapshot captured when the action was chosen.
        assert.equal(contacts.find(c => c.type === 'group').unread, 1);

        // A failed clear leaves the badge visible and the button usable.
        failReads = true;
        await page.evaluate(() => markAllContactsRead());
        assert.equal(await page.locator('.unread-badge').count(), 1);
        assert.match(await page.locator('#contactsFeedback').innerText(), /失败/);
        assert.equal(await page.locator('#btnMarkAllRead').isDisabled(), false);
        failReads = false;

        // Receipts saved during browser back are flushed before the list is rendered.
        const group = contacts.find(c => c.type === 'group');
        await page.evaluate(item => ReadReceipts.queue(1, item), {type: 'group', id: 'hero', last_message_id: group.last_message_id});
        await page.evaluate(() => loadContacts());
        assert.equal(await page.locator('.unread-badge').count(), 0);
        assert.equal(await page.locator('#btnMarkAllRead').isDisabled(), true);

        // Fake browser time verifies 10-second polling and pause/resume without a long wait.
        await page.clock.install();
        await page.evaluate(() => scheduleContactsRefresh());
        let beforeGets = gets;
        await page.clock.fastForward(10001);
        await page.evaluate(() => contactsRequest);
        assert(gets > beforeGets);
        await page.evaluate(() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
            document.dispatchEvent(new Event('visibilitychange'));
        });
        beforeGets = gets;
        await page.clock.fastForward(30000);
        assert.equal(gets, beforeGets);
        await page.evaluate(() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
            document.dispatchEvent(new Event('visibilitychange'));
            return contactsRequest;
        });
        assert(gets > beforeGets);

        // Exercise the actual chat read logic, with persisted IDs and browser visibility.
        await page.goto('http://kunigami.test/chat-harness');
        const chatSource = source('templates/chat.html');
        const latestIdFunction = chatSource.slice(chatSource.indexOf('    function latestPersistedMessageId()'), chatSource.indexOf('    function pollNewMessages()'));
        const readLogic = chatSource.slice(chatSource.indexOf('  // Only advance through persisted messages'), chatSource.indexOf('  // 【新增】图片导出相关'));
        await page.addScriptTag({content: `const chatContainer = document.getElementById('chatContainer'); let isLoading = false; const currentUserId = 1; const isGroupMode = true; const currentId = 'hero';\n${latestIdFunction}\n${readLogic}`});
        await page.evaluate(() => {
            chatContainer.innerHTML = '<div class="message-group" data-id="122">消息</div>';
            captureDisplayedReadId();
            return markAsRead();
        });
        assert.equal(singles.at(-1).last_message_id, 122);
        const beforeSingles = singles.length;
        await page.evaluate(() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
            chatContainer.innerHTML += '<div class="message-group" data-id="123">后台新消息</div>';
            captureDisplayedReadId();
            return markAsRead();
        });
        assert.equal(singles.length, beforeSingles);
        await page.evaluate(() => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
            captureDisplayedReadId();
            return markAsRead();
        });
        assert.equal(singles.at(-1).last_message_id, 123);
        assert.deepEqual(errors, []);
        console.log('PASS: mobile/desktop badge layout, sweep snapshots, multiselect preservation, failed saves, back retries, foreground polling and chat read receipts');
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
