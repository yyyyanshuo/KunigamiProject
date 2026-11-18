/* Real Chromium, production render functions, synthetic fixtures, no live server. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'templates/chat.html'), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, start);
  return source.slice(a, b);
};
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    const context = await browser.newContext({serviceWorkers: 'block'});
    const unexpected = [];
    await context.route('**/*', route => {
      if (route.request().url() === 'http://saki.test/') return route.fulfill({contentType: 'text/html', body:
        '<!doctype html><html lang="en"><head></head><body><div id="chatContainer"></div><div id="searchResults"></div><div id="thoughtsModalBody"></div><div id="thoughtsModalOverlay"></div></body></html>'});
      const url = route.request().url();
      if (url.includes('blocked.invalid') || url.includes('/api/private')) unexpected.push(url);
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://saki.test/');
    await page.evaluate(() => {localStorage.setItem('synthetic-private', 'A_ONLY'); window.__executed = false;});
    await page.evaluate(() => {
      for (const id of ['forumModalTitle', 'forumModalBody', 'forumModalOverlay', 'chatlogModalTitle', 'chatlogModalBody', 'chatlogModalOverlay']) {
        const el = document.createElement('div'); el.id = id; document.body.append(el);
      }
    });
    for (const file of ['ui-language.js', 'image-tags.js', 'message-tags.js', 'safe-chat-renderer.js']) {
      await page.addScriptTag({path: path.join(root, 'static', file)});
    }
    await page.addScriptTag({content: `
      const chatContainer = document.getElementById('chatContainer');
      const searchResults = document.getElementById('searchResults');
      let isGroupMode = false;
      const currentConfig = {}, charConfig = {}, groupMembers = {}, contactsCache = {}, currentUserName = 'User';
      const currentId = 'demo', aiLanguage = 'en', copyIconPath = '', RECORD_TIMEZONE = 'Asia/Shanghai';
      const extractCruiseFromContent = cleanText => ({cleanText, cruises: []});
      const parseChatTimestamp = timestamp => new Date(timestamp);
      const bindCompactMessageActions = () => {};
      const refreshDateSeparators = () => {};
      const triggerAgent = url => {window.agentTarget = url;};
      const jumpToMessage = id => {window.jumpedTo = id;};
      const resolveAvatar = () => '/static/default_avatar.png';
      const resolveDisplayName = value => value;
      ${section('  function extractTicklesFromContent(text)', '  // 只把 HTML 标签和 URL 之外的斜线')}
      ${section('  function splitForwardedChatlogSegments(text)', '  function closeForumModal()')}
      ${section('  function needsReadingTranslation()', '  function legacyGenerationErrorMessage(text)')}
      const legacyGenerationErrorMessage = () => '';
      ${section('  function stripRubyTags(text)', '  async function openEditModal(')}
      ${section('  function extractSafetyAlertFromContent(text)', '  function parseTransferMessageSegment(text)')}
      ${section('  function parseTransferMessageSegment(text)', '  function replaceRenderedMessageGroup(message)')}
      ${section('  function replaceRenderedMessageGroup(message)', '  async function refreshRenderedMessageById(')}
      ${section('  function renderSystemPromptGroup(', '    // --- 【新增】全局日期重排函数')}
      ${section('    function renderSearchResults(list, kw)', '  // --- 核心：跳转定位')}
      function createHtmlBlock(html) {return SakiSafeChat.createHtmlBlock(html);}
    `});
    const attack = '<img src="https://blocked.invalid/x" onerror="window.__executed=true"><svg onload="window.__executed=true"></svg>';
    for (const groupMode of [false, true]) for (const mode of ['append', 'prepend']) {
      await page.evaluate(({groupMode, mode, attack}) => {
        isGroupMode = groupMode;
        chatContainer.replaceChildren();
        renderMessageGroup({id: 1, role: 'user', content: attack, timestamp: '2026-10-16T12:00:00Z'}, mode);
      }, {groupMode, mode, attack});
      assert.equal(await page.locator('.bubble img,.bubble svg').count(), 0);
      assert((await page.locator('.bubble').allTextContents()).join('').includes('<img'));
      await page.evaluate(attack => replaceRenderedMessageGroup({id: 1, content: attack + ' edited'}), attack);
      assert.equal(await page.locator('.bubble img,.bubble svg').count(), 0);
    }
    await page.evaluate(attack => {
      isGroupMode = false;
      renderMessageGroup({id: 2, role: 'assistant', content: '[THOUGHTS]' + attack + '[/THOUGHTS]', timestamp: '2026-10-16T12:00:00Z'});
      openThoughtsModal(document.querySelector('[data-id="2"]'), 0);
      renderSearchResults([{id: 1, role: 'user', content: attack, timestamp: '2026-10-16T12:00:00Z'}], 'img');
      renderMessageGroup({id: 3, role: 'assistant', content: '你好 Hello <ruby>学校<rt>がっこう</rt></ruby> 😊', timestamp: '2026-10-16T12:00:00Z'});
      renderMessageGroup({id: 4, role: 'assistant', content: '[TRANSFER:100円|note]', timestamp: '2026-10-16T12:00:00Z'});
      renderMessageGroup({id: 40, role: 'assistant', content: '[TRANSFER:100€|Pour tes cafés.]', timestamp: '2026-10-29T12:00:00Z'});
      renderMessageGroup({id: 5, role: 'user', content: '[voice_message](sample.webm)(Hello)', timestamp: '2026-10-16T12:00:00Z'});
      renderMessageGroup({id: 50, role: 'assistant', content: '[VOICE](Hello)(softly)', timestamp: '2026-10-16T12:00:00Z'});
      renderMessageGroup({id: 6, role: 'assistant', content: '[IMAGE](/static/synthetic.png)(Synthetic image)', timestamp: '2026-10-16T12:00:00Z'});
      renderMessageGroup({id: 7, role: 'assistant', content: '[STICKER]official:synthetic.png', timestamp: '2026-10-16T12:00:00Z'});
      renderMessageGroup({id: 8, role: 'assistant', content: '[FORUM:Title][POST:Author:npc:0]' + attack + '[/POST][/FORUM]', timestamp: '2026-10-16T12:00:00Z'});
      openForumModal(document.querySelector('[data-id="8"]'), 0);
      renderMessageGroup({id: 9, role: 'assistant', content: '[CHATLOG:demo][DATE:2026-10-16][LOG:user|12:00]' + attack + '[/LOG][/CHATLOG]', timestamp: '2026-10-16T12:00:00Z'});
      openChatlogModal(document.querySelector('[data-id="9"]'), 0);
      const extra = document.createElement('div'); extra.id = 'extra'; document.body.append(extra);
      SakiSafeChat.renderMessage(extra, 'https://example.invalid/a?b=1 [ASK:<img onerror=bad>] https://%invalid', {onAgent: triggerAgent});
    }, attack);
    assert.equal(await page.locator('#thoughtsModalBody img,.thoughts-card-preview img,#searchResults img').count(), 0);
    assert.equal(await page.locator('[data-id="3"] ruby').count(), 1);
    assert.equal(await page.locator('[data-id="4"] .transfer-card').count(), 1);
    assert.equal(await page.locator('[data-id="40"] .transfer-card').count(), 1);
    assert((await page.locator('[data-id="40"] .transfer-card').textContent()).includes('100€'));
    assert((await page.locator('[data-id="40"] .transfer-card').textContent()).includes('Pour tes cafés.'));
    assert((await page.locator('[data-id="5"]').textContent()).includes('Hello'));
    assert.equal(await page.locator('[data-id="50"] .bubble-voice .voice-play-btn').count(), 1);
    assert((await page.locator('[data-id="6"] .bubble img').count()) > 0);
    assert((await page.locator('[data-id="7"] .bubble img').count()) > 0);
    assert.equal(await page.locator('.forum-post-content img,.chatlog-log-content img').count(), 0);
    assert((await page.locator('.forum-post-content').textContent()).includes('<img'));
    assert((await page.locator('.chatlog-log-content').textContent()).includes('<img'));
    assert.equal(await page.locator('#extra [onclick],#extra img').count(), 0);
    assert.equal(await page.locator('#extra a').getAttribute('rel'), 'noopener noreferrer');
    await page.locator('#extra .agent-btn').click();
    assert.equal(await page.evaluate(() => agentTarget), 'https://example.invalid/a?b=1');
    assert.equal(await page.evaluate(() => __executed), false);

    await page.evaluate(() => {
      const html = `<style>body{color:rgb(1,2,3)} .exfil{background:url(https://blocked.invalid/css)}</style>
        <details><summary>Open</summary><p style="height:300px">Native detail</p></details><input id="native" value="hello">
        <div class="exfil"></div><img src="https://blocked.invalid/image">
        <iframe src="https://blocked.invalid/nested"></iframe>
        <form action="https://blocked.invalid/form"><button id="submit">submit</button></form>
        <a id="navigate" href="https://blocked.invalid/navigation">navigate</a>
        <script>window.untrustedRan=true;parent.__executed=true;fetch('/api/private',{credentials:'include'});<\/script>
        <button id="handler" onclick="window.untrustedRan=true">event</button>`;
      const result = createHtmlBlock(html);
      result.iframe.id = 'isolated'; document.body.append(result.wrapper);
    });
    const frame = page.frameLocator('#isolated');
    await frame.locator('#native').fill('native input works');
    await frame.locator('summary').click();
    assert.equal(await frame.locator('details').getAttribute('open'), '');
    assert.equal(await frame.locator('#native').inputValue(), 'native input works');
    await frame.locator('#handler').click();
    const childFrame = await (await page.locator('#isolated').elementHandle()).contentFrame();
    assert.deepEqual(await childFrame.evaluate(() => {
      let parentBlocked = false, storageBlocked = false;
      try {void parent.document.body;} catch (_) {parentBlocked = true;}
      try {void localStorage.getItem('synthetic-private');} catch (_) {storageBlocked = true;}
      return {parentBlocked, storageBlocked, untrustedRan: Boolean(window.untrustedRan), color: getComputedStyle(document.body).color};
    }), {parentBlocked: true, storageBlocked: true, untrustedRan: false, color: 'rgb(1, 2, 3)'});
    await page.waitForTimeout(250);
    assert.notEqual(await page.locator('#isolated').evaluate(el => el.style.height), '150px');
    const bridge = await page.evaluate(() => {
      const el = document.getElementById('isolated'), height = el.style.height;
      const token = el.srcdoc.match(/nonce="([a-f0-9]+)"/)[1];
      for (const [source, value] of [[window, 800], [el.contentWindow, 999999], [el.contentWindow, '900']]) {
        dispatchEvent(new MessageEvent('message', {source, origin: 'null', data: {type: 'sakiHtmlResize', token, height: value}}));
      }
      return el.style.height === height;
    });
    assert(bridge);
    await frame.locator('#submit').click();
    await frame.locator('#navigate').click();
    await page.waitForTimeout(100);
    assert.equal(page.url(), 'http://saki.test/');
    assert.equal(await page.evaluate(() => __executed), false);
    assert.deepEqual(unexpected, [], 'CSP must reject resource/navigation requests before routing');
    assert.deepEqual(errors, []);
    console.log('PASS Chromium: real append/history/edit/search/thought/forum/chatlog rendering, ruby, links, image/sticker/transfer/voice cards; native HTML controls, styles, script/storage/parent/network isolation, bounded resize');
  } finally {await browser.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
