const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const source = fs.readFileSync(path.join(__dirname, '../templates/chat.html'), 'utf8');
const section = (start, end) => {
    const a = source.indexOf(start), b = source.indexOf(end, a);
    assert(a >= 0 && b > a);
    return source.slice(a, b);
};
const header = section('<header class="chat-header">', '</header>') + '</header>';
const styles = Array.from(source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g), m => m[1]).join('\n');
for (const m of source.replace(/<!--[\s\S]*?-->/g, '').matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
    new vm.Script(m[1].replace(/\{\{[\s\S]*?\}\}/g, '1').replace(/\{%[\s\S]*?%\}/g, ''));
}

async function checkHeaderLayout(page, groupMode) {
    const name = page.locator('.header-name');
    let expectedHeight;
    for (const [width, label] of [
        [375, '玲王'], [375, 'ミヒャエル・カイザー'], [768, 'ミヒャエル・カイザー'],
        [320, 'TrainingPartners'.repeat(4) + ' (12人)']
    ]) {
        await page.setViewportSize({width, height: 740});
        await name.evaluate((el, text) => { el.textContent = text; }, label);
        const metrics = await page.locator('.chat-header').evaluate(el => {
            const rect = selector => el.querySelector(selector).getBoundingClientRect();
            const area = el.getBoundingClientRect(), name = rect('.header-name'), status = rect('.header-status');
            const back = rect('#chatBackBtn'), search = rect('.header-left-group button:nth-child(2)');
            const right = rect('.header-right-group');
            const centered = box => Math.abs((box.left + box.right - area.left - area.right) / 2) < 1;
            return {
                centered: centered(name), statusCentered: centered(status), height: area.height,
                leftInset: back.left - area.left, rightInset: area.right - right.right, gap: search.left - back.right,
                leftIconInset: rect('#chatBackBtn svg').left - area.left,
                rightIconInset: area.right - rect('.header-right-group button:last-child svg').right,
                minTarget: Math.min(back.width, back.height, search.width, search.height),
                sameRow: name.top >= area.top && name.bottom <= area.bottom && name.top < right.bottom,
                statusBelow: status.top >= name.bottom && status.bottom <= area.bottom,
                overlaps: name.right > right.left && name.left < right.right,
                fullWidth: el.querySelector('.header-name').scrollWidth <= name.width + 1,
                clickable: Array.from(el.querySelectorAll('button')).every(btn => {
                    const r = btn.getBoundingClientRect(), y = (r.top + r.bottom) / 2;
                    return [r.left + 2, (r.left + r.right) / 2, r.right - 2].every(x =>
                        document.elementFromPoint(x, y)?.closest('button') === btn);
                })
            };
        });
        expectedHeight ??= metrics.height;
        assert.equal(metrics.height, expectedHeight, 'long names must not take message space');
        assert(metrics.centered && (groupMode || (metrics.statusCentered && metrics.statusBelow)));
        assert.equal(metrics.leftInset, metrics.rightInset);
        assert.equal(metrics.leftIconInset, metrics.rightIconInset);
        assert.equal(metrics.gap, 0);
        assert(metrics.minTarget >= 40, 'compact controls retain individual touch targets');
        assert(metrics.sameRow && metrics.fullWidth && metrics.clickable, JSON.stringify(metrics));
        if (width === 320) assert(metrics.overlaps, 'legacy long name runs underneath the buttons');
    }
}

(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
    try {
        for (const groupMode of [false, true]) {
            const page = await browser.newPage({viewport: {width: 320, height: 640}});
            await page.route('**/*', route => route.fulfill({contentType: 'text/html; charset=utf-8', body: `<style>${styles}</style>${header}<input id="input"><button id="send">发送</button><div id="chatContainer" style="height:400px;overflow:auto"></div>`}));
            await page.goto('http://kunigami.test/');
            await page.addScriptTag({content: `
                const SakiI18n = {t: (text, values = {}) => text.replace('{count}', values.count)};
                const CHAT_TRANS_CACHE_PREFIX = 'synthetic:translations:1:', CHAT_TRANS_HIDDEN_KEY = CHAT_TRANS_CACHE_PREFIX + 'hidden';
                const input = document.getElementById('input'), btn = document.getElementById('send'), chatContainer = document.getElementById('chatContainer');
                const isGroupMode = ${groupMode}, currentId = 'hero', charId = 'hero', currentUserId = 1;
                let pendingUserTransSource = null, isLoading = false, isBrowsingHistory = false, historyViewVersion = 0, totalMessages = 0;
                const pendingSentTranslations = new Map(), chatHiddenTrans = new Set();
                let agentIsWaiting = false, agentIsRunning = false;
                const messagesPerPage = 20;
                let newMessagesRequest = null;
                window.latestReturns = 0;
                const returnToLatestMessages = async () => { latestReturns++; isBrowsingHistory = false; return true; };
                window.db = []; window.persistCalls = 0; window.generationCalls = []; window.pollCalls = 0;
                window.failPersist = false; window.failGeneration = false; window.replies = false;
                window.activePolls = 0; window.peakPolls = 0;
                window.headerState = {deep_sleep: true, unread_total: 1234};
                const getApiPrefix = () => isGroupMode ? '/api/group/hero' : '/api/hero';
                const randomizeStickersInText = async text => text;
                const hasWebCommands = () => false;
                const handleApiUnauthorized = () => false;
                const refreshDateSeparators = () => {};
                function generationErrorPayload(e) { return {message: e.message}; }
                function renderGenerationErrorGroup(payload) { return renderMessageGroup({role:'assistant', content:payload.message}); }
                function renderMessageGroup(msg) {
                    const el = document.createElement('div'); el.className = 'message-group ' + msg.role;
                    if (msg.id != null) el.dataset.id = String(msg.id);
                    el.innerHTML = '<div class="message-wrapper"><div class="bubble-stack"></div></div>';
                    el.querySelector('.bubble-stack').textContent = msg.content;
                    chatContainer.appendChild(el); return el;
                }
                async function fetchWithTimeout(url, options) {
                    const payload = JSON.parse(options.body);
                    if (url.endsWith('/messages')) {
                        window.persistCalls++;
                        if (window.failPersist) return {ok:false, json:async()=>({error:'failed'})};
                        const item = {id:db.length+1, content:payload.message, role:'user', timestamp:'2026-09-05 18:00:00'};
                        db.push(item);
                        // Deliberately expose the DB row to polling before the persist response.
                        await new Promise(resolve => window.releasePersist = resolve);
                        return {ok:true, json:async()=>({user_id:item.id, timestamp:item.timestamp})};
                    }
                    generationCalls.push(payload);
                    await new Promise(resolve => window.releaseGeneration = resolve);
                    if (window.failGeneration) return {ok:false, json:async()=>({error:'AI timeout'})};
                    if (window.replies) db.push({id:db.length+1, content:'reply', role:'assistant', timestamp:'2026-09-05 18:00:01'});
                    return {ok:true, json:async()=>({replies:[]})};
                }
                async function fetchWithAuthGuard(url) {
                    pollCalls++; peakPolls = Math.max(peakPolls, ++activePolls);
                    const params = new URL(url, location.origin).searchParams;
                    if (params.get('header') !== '1') throw new Error('Missing poll metadata request');
                    const after = Number(params.get('after_id')) || 0;
                    const messages = db.filter(m=>m.id > after).map(m=>({...m}));
                    await new Promise(resolve => setTimeout(resolve, 10)); activePolls--;
                    return {ok:true, json:async()=>({messages,total:db.length,header:headerState})};
                }
                ${section('    async function sendMessage()', '    // ==============================')}
                ${section('    function latestPersistedMessageId()', '    // 启动轮询')}
                ${section('  function _applyPendingTranslation()', '  async function translateMessage')}
            `});
            await page.evaluate(() => pollNewMessages());
            assert.equal(await page.locator('#chatUnreadTotal').innerText(), '1234');
            assert((await page.locator('#chatBackBtn').getAttribute('aria-label')).includes('1234'));
            assert.equal(await page.locator('.header-status').isVisible(), !groupMode);
            if (!groupMode) assert.match(await page.locator('.header-status').innerText(), /睡眠/);
            await checkHeaderLayout(page, groupMode);
            await page.evaluate(() => { headerState = {deep_sleep: false}; return pollNewMessages(); });
            assert(await page.locator('#chatUnreadTotal').isHidden(), 'a missing count must clear the previous badge');
            assert(!(await page.locator('#chatBackBtn').getAttribute('aria-label')).includes('1234'));
            if (!groupMode) assert.match(await page.locator('.header-status').innerText(), /手机在线/);
            const badge = page.locator('#chatUnreadTotal');
            const search = page.locator('.header-left-group button:nth-child(2)');
            const searchPosition = await search.boundingBox();
            for (const count of [0, 7, 12, 1234, 123456, 0]) {
                await page.evaluate(n => { headerState = {unread_total: n}; return pollNewMessages(); }, count);
                assert.deepEqual(await search.boundingBox(), searchPosition, 'search keeps the same position');
                if (!count) {
                    assert(await badge.isHidden());
                    continue;
                }
                assert.equal(await badge.innerText(), String(count));
                const box = await badge.boundingBox();
                assert(box && Math.abs(box.width - box.height) < 1, 'unread badge must stay circular');
                assert(box.x + box.width <= searchPosition.x, 'circle fits before search');
                assert(await badge.evaluate(el => {
                    const css = getComputedStyle(el);
                    return css.borderRadius === '50%' && css.borderTopStyle === 'solid' &&
                        css.backgroundColor !== 'rgba(0, 0, 0, 0)' && css.backgroundColor !== css.borderTopColor &&
                        el.scrollWidth <= el.clientWidth + 1;
                }), 'show the full count inside a filled circle with a distinct outline');
            }
            await page.evaluate(() => { headerState = {unread_total: 0}; return pollNewMessages(); });
            assert(await page.locator('#chatUnreadTotal').isHidden());
            await page.evaluate(() => {
                input.value = 'translated message'; pendingUserTransSource = '中文原文';
                window.sending = sendMessage();
            });
            await page.waitForFunction(() => db.length === 1);
            assert.equal(await page.locator('.message-group.user').count(), 0, 'send must not create a local bubble');
            await page.evaluate(() => Promise.all([pollNewMessages(), pollNewMessages()]));
            assert.equal(await page.locator('.message-group.user').count(), 1, 'empty chat must receive the first persisted row');
            assert.equal(await page.evaluate(() => peakPolls), 1);
            await page.evaluate(() => { releasePersist(); });
            await page.waitForFunction(() => generationCalls.length === 1);
            assert.equal(await page.locator('.message-group.user').count(), 1, 'persist response must not insert a second bubble');
            assert.equal(await page.locator('.translation-source').innerText(), '中文原文');
            assert.equal(await page.evaluate(() => generationCalls[0].user_message_id), 1);
            // Enter/double-click while sending must not persist another copy.
            await page.evaluate(() => { input.value = 'double click'; return sendMessage(); });
            assert.equal(await page.evaluate(() => persistCalls), 1);
            await page.evaluate(() => { releaseGeneration(); return sending; });
            await page.evaluate(() => pollNewMessages());
            assert.equal(await page.locator('.message-group.user').count(), 1);
            assert.equal(await page.locator('.translation-source').count(), 1);
            // Simulate history being reconstructed on reload: translation uses the real ID.
            await page.evaluate(() => { chatContainer.innerHTML = ''; db.forEach(renderMessageGroup); showCachedTranslations(); });
            assert.equal(await page.locator('.translation-source').innerText(), '中文原文');
            // Generation errors keep the one persisted user message and its translation.
            await page.evaluate(() => {
                input.value = 'second translated message'; pendingUserTransSource = '第二条原文';
                isBrowsingHistory = true;
                failGeneration = true; window.sending = sendMessage();
            });
            await page.waitForFunction(() => db.length === 2);
            await page.evaluate(() => { releasePersist(); });
            await page.waitForFunction(() => generationCalls.length === 2);
            await page.evaluate(() => { releaseGeneration(); return sending; });
            await page.evaluate(() => pollNewMessages());
            assert.equal(await page.locator('.message-group.user').count(), 2);
            assert.equal(await page.evaluate(() => latestReturns), 1, 'sending from history must return to the latest view');
            assert.equal(await page.locator('.message-group.user[data-id="2"] .translation-source').innerText(), '第二条原文');
            // A failed persist never leaves a ghost user bubble.
            await page.evaluate(() => { failPersist = true; input.value = 'failed'; return sendMessage(); });
            assert.equal(await page.locator('.message-group.user').count(), 2);
            assert.equal(await page.evaluate(() => btn.disabled), false);
            await page.close();
        }
        console.log('PASS: private/group send-poll races, first message in empty chat, single bubble, real-ID translation/reload, duplicate-submit guard and failure paths');
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
