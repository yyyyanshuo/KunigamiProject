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
for (const m of source.replace(/<!--[\s\S]*?-->/g, '').matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
    new vm.Script(m[1].replace(/\{\{[\s\S]*?\}\}/g, '1').replace(/\{%[\s\S]*?%\}/g, ''));
}

(async () => {
    const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
    try {
        for (const groupMode of [false, true]) {
            const page = await browser.newPage();
            await page.route('**/*', route => route.fulfill({contentType: 'text/html', body: '<input id="input"><button id="send">发送</button><div id="chatContainer" style="height:400px;overflow:auto"></div>'}));
            await page.goto('http://kunigami.test/');
            await page.addScriptTag({content: `
                const input = document.getElementById('input'), btn = document.getElementById('send'), chatContainer = document.getElementById('chatContainer');
                const isGroupMode = ${groupMode}, currentId = 'hero', charId = 'hero', currentUserId = 1;
                let pendingUserTransSource = null, isLoading = false, totalMessages = 0;
                const pendingSentTranslations = new Map(), chatHiddenTrans = new Set();
                let agentIsWaiting = false, agentIsRunning = false;
                const messagesPerPage = 20;
                let newMessagesRequest = null;
                window.db = []; window.persistCalls = 0; window.generationCalls = []; window.pollCalls = 0;
                window.failPersist = false; window.failGeneration = false; window.replies = false;
                window.activePolls = 0; window.peakPolls = 0;
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
                    const after = Number(new URL(url, location.origin).searchParams.get('after_id')) || 0;
                    const messages = db.filter(m=>m.id > after).map(m=>({...m}));
                    await new Promise(resolve => setTimeout(resolve, 10)); activePolls--;
                    return {ok:true, json:async()=>({messages,total:db.length})};
                }
                ${section('    async function sendMessage()', '    // ==============================')}
                ${section('    function latestPersistedMessageId()', '    // 启动轮询')}
                ${section('  function _applyPendingTranslation()', '  async function translateMessage')}
            `});
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
            assert.equal(await page.locator('.translation-result').innerText(), '中文原文');
            assert.equal(await page.evaluate(() => generationCalls[0].user_message_id), 1);
            // Enter/double-click while sending must not persist another copy.
            await page.evaluate(() => { input.value = 'double click'; return sendMessage(); });
            assert.equal(await page.evaluate(() => persistCalls), 1);
            await page.evaluate(() => { releaseGeneration(); return sending; });
            await page.evaluate(() => pollNewMessages());
            assert.equal(await page.locator('.message-group.user').count(), 1);
            assert.equal(await page.locator('.translation-result').count(), 1);
            // Simulate history being reconstructed on reload: translation uses the real ID.
            await page.evaluate(() => { chatContainer.innerHTML = ''; db.forEach(renderMessageGroup); showCachedTranslations(); });
            assert.equal(await page.locator('.translation-result').innerText(), '中文原文');
            // Generation errors keep the one persisted user message and its translation.
            await page.evaluate(() => {
                input.value = 'second translated message'; pendingUserTransSource = '第二条原文';
                failGeneration = true; window.sending = sendMessage();
            });
            await page.waitForFunction(() => db.length === 2);
            await page.evaluate(() => { releasePersist(); });
            await page.waitForFunction(() => generationCalls.length === 2);
            await page.evaluate(() => { releaseGeneration(); return sending; });
            await page.evaluate(() => pollNewMessages());
            assert.equal(await page.locator('.message-group.user').count(), 2);
            assert.equal(await page.locator('.message-group.user[data-id="2"] .translation-result').innerText(), '第二条原文');
            // A failed persist never leaves a ghost user bubble.
            await page.evaluate(() => { failPersist = true; input.value = 'failed'; return sendMessage(); });
            assert.equal(await page.locator('.message-group.user').count(), 2);
            assert.equal(await page.evaluate(() => btn.disabled), false);
            await page.close();
        }
        console.log('PASS: private/group send-poll races, first message in empty chat, single bubble, real-ID translation/reload, duplicate-submit guard and failure paths');
    } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
