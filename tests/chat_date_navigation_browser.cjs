/* Real navigation controls and history loaders; synthetic message fixtures only. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const source = fs.readFileSync(path.join(__dirname, '../templates/chat.html'), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, start); return source.slice(a, b);
};
const css = Array.from(source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g), m => m[1]).join('\n');
const modal = section('<div id="searchModal"', '<!-- 【论坛体】弹窗 -->');
const bar = section('<div id="historyBrowseBar"', '<!-- 【新增】加载更多历史记录的按钮 -->');
const ids = page => page.locator('#chatContainer .message-group').evaluateAll(nodes => nodes.map(n => Number(n.dataset.id)));

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    for (const groupMode of [false, true]) {
      const page = await browser.newPage({viewport: {width: groupMode ? 1280 : 390, height: 844}});
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => route.fulfill({contentType: 'text/html; charset=utf-8', body:
        `<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><style>${css}
        #chatContainer {height:400px;flex:none;overflow:auto} .message-group {height:80px;flex-shrink:0}</style>
        <button id="openSearch" onclick="openSearchModal()">搜索</button>${bar}
        <div id="load-more-container"><button id="loadMoreBtn">更早</button></div>
        <div id="chatContainer"></div><input id="messageInput"><button id="sendBtn"></button>${modal}</html>`}));
      await page.goto('http://saki.test/');
      await page.addScriptTag({content: `
        const SakiI18n = {t: (s, values={}) => Object.entries(values).reduce((text,[key,value]) => text.replace('{'+key+'}',value), s), fragment: s => s};
        const RECORD_TIMEZONE = 'Asia/Tokyo';
        const recordDateKey = date => new Intl.DateTimeFormat('en-CA', {timeZone:RECORD_TIMEZONE, year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
        const chatContainer = document.getElementById('chatContainer'), btn = document.getElementById('sendBtn');
        const input = document.getElementById('messageInput');
        const loadMoreBtn = document.getElementById('loadMoreBtn'), loadMoreContainer = document.getElementById('load-more-container');
        let isLoading = false, isBrowsingHistory = false, historyHasMoreBelow = false, historyViewVersion = 0;
        let totalMessages = 100, newMessagesRequest = null;
        const messagesPerPage = 20;
        const getApiPrefix = () => '${groupMode ? '/api/group/demo' : '/api/demo'}';
        const handleApiUnauthorized = () => false;
        const showCachedTranslations = () => {}, refreshDateSeparators = () => {}, _applyPendingTranslation = () => {};
        const checkAndAppendDateSeparator = () => {}, sendMessage = () => {};
        const showChatDiagnostic = data => {throw new Error(JSON.stringify(data));}, formatDiagnosticError = e => e.message;
        const renderMessageGroup = (msg, mode) => {
          const el = document.createElement('div'); el.className = 'message-group';
          el.dataset.id = msg.id; el.textContent = msg.content;
          if(mode === 'prepend') chatContainer.prepend(el); else chatContainer.append(el);
          return el;
        };
        window.db = Array.from({length:100}, (_,i) => ({id:i+1,role:'user',content:'Message '+(i+1),timestamp:'2026-10-30 12:00:00'}));
        window.requests = []; window.delayDate = false; window.holdPoll = false; window.failDate = false;
        async function fetchWithAuthGuard(url) {
          requests.push(url);
          const q = new URL(url, location.origin).searchParams;
          let messages, target = Number(q.get('target_id')) || null;
          if(q.has('target_date')) target = q.get('target_date') === '2026-10-30' ? 40 : null;
          if(q.has('target_date') && !target) messages = [];
          else if(target) messages = db.filter(m => m.id >= target-5 && m.id <= target+5);
          else if(q.has('before_id')) messages = db.filter(m => m.id < Number(q.get('before_id'))).slice(-Number(q.get('limit')));
          else if(q.has('after_id')) messages = db.filter(m => m.id > Number(q.get('after_id'))).slice(0, Number(q.get('limit')));
          else messages = db.slice(-Number(q.get('limit')));
          const data = {messages, target_id:target, total:db.length,
            has_before:!!messages.length && messages[0].id > 1,
            has_after:!!messages.length && messages[messages.length-1].id < db.length};
          if(q.has('target_date') && delayDate) await new Promise(resolve => window.releaseDate = resolve);
          if(q.get('limit') === '9999' && holdPoll) await new Promise(resolve => window.releasePoll = resolve);
          return {ok:!(q.has('target_date') && failDate), status:failDate ? 500 : 200,
            json:async()=>data, text:async()=>JSON.stringify(data), headers:{get:()=> 'application/json'}};
        }
        ${section('  async function loadHistory(mode', '  // --- 键盘弹起优化 ---')}
        ${section('  // --- 键盘弹起优化 ---', '    // --- 通用 fetch 超时封装')}
        ${section("    loadMoreBtn.addEventListener('click'", "    btn.addEventListener('click'")}
        ${section('    function latestPersistedMessageId()', '    // 启动轮询')}
        ${section('    // --- 搜索与定位功能 ---', '    // --- 常用语功能 ---')}
        db.slice(-20).forEach(msg => renderMessageGroup(msg));
        chatContainer.scrollTop = chatContainer.scrollHeight;
      `});

      // A poll already in flight must not append its old page after a date jump.
      await page.evaluate(() => {
        db.push({id:101,role:'user',content:'New incoming message'});
        holdPoll = true; window.pendingPoll = pollNewMessages();
      });
      await page.waitForFunction(() => typeof releasePoll === 'function');
      await page.click('#openSearch');
      assert((await page.locator('#historyDateTimezone').innerText()).includes('Asia/Tokyo'));
      assert(await page.locator('#historyDateInput').evaluate(el => el.getBoundingClientRect().right <= innerWidth));
      await page.fill('#historyDateInput', '2026-10-30');
      await page.evaluate(() => { delayDate = true; });
      await page.click('#historyDateJumpBtn');
      await page.waitForFunction(() => typeof releaseDate === 'function');
      assert(await page.locator('#historyDateJumpBtn').isDisabled());
      await page.evaluate(() => document.getElementById('historyDateForm').dispatchEvent(new Event('submit', {cancelable:true})));
      assert.equal(await page.evaluate(() => requests.filter(url=>url.includes('target_date')).length), 1);
      await page.evaluate(() => { delayDate = false; releaseDate(); });
      await page.waitForFunction(() => isBrowsingHistory && !isLoading);
      assert.deepEqual(await ids(page), Array.from({length:11},(_,i)=>i+35));
      assert(await page.locator('#historyBrowseBar').isVisible());
      await page.evaluate(() => { holdPoll = false; releasePoll(); return pendingPoll; });
      const count = await page.evaluate(() => requests.length);
      await page.evaluate(() => pollNewMessages());
      assert.equal(await page.evaluate(() => requests.length), count + 1);
      assert((await page.evaluate(() => requests.at(-1))).endsWith('/history?header=only'));
      assert.deepEqual(await ids(page), Array.from({length:11},(_,i)=>i+35));
      assert(await page.locator('[data-id="40"]').evaluate(el => {
        const r = el.getBoundingClientRect(), c = el.parentElement.getBoundingClientRect();
        return r.top >= c.top && r.bottom <= c.bottom;
      }));
      const historyScrollTop = await page.evaluate(() => chatContainer.scrollTop);
      await page.evaluate(() => visualViewport.dispatchEvent(new Event('resize')));
      await page.waitForTimeout(150);
      assert.equal(await page.evaluate(() => chatContainer.scrollTop), historyScrollTop, 'keyboard resize must retain the date anchor');

      // The legacy button is CSS-hidden; its handler still uses the top cursor.
      await page.evaluate(() => document.getElementById('loadMoreBtn').click());
      await page.waitForFunction(() => !isLoading && !!document.querySelector('[data-id="15"]'));
      assert.equal(await page.evaluate(() => requests[requests.length-1].includes('before_id=35')), true);
      await page.evaluate(() => { chatContainer.scrollTop = chatContainer.scrollHeight; chatContainer.dispatchEvent(new Event('scroll')); });
      await page.waitForFunction(() => !isLoading && !!document.querySelector('[data-id="65"]'));
      assert.deepEqual(await ids(page), Array.from({length:51},(_,i)=>i+15));

      // Missing dates and HTTP errors keep both the messages and the modal available.
      const before = await ids(page);
      await page.click('#openSearch');
      await page.fill('#historyDateInput', '2026-01-01');
      await page.click('#historyDateJumpBtn');
      await page.waitForFunction(() => document.getElementById('historyDateStatus').textContent.includes('没有聊天记录'));
      assert.deepEqual(await ids(page), before);
      await page.fill('#historyDateInput', '2026-10-30');
      await page.evaluate(() => { failDate = true; });
      await page.click('#historyDateJumpBtn');
      await page.waitForFunction(() => document.getElementById('historyDateStatus').textContent.includes('跳转失败'));
      assert.deepEqual(await ids(page), before);
      await page.evaluate(() => { failDate = false; closeSearchModal(); });

      await page.click('#returnToLatestBtn');
      await page.waitForFunction(() => !isLoading && !isBrowsingHistory);
      assert.deepEqual(await ids(page), Array.from({length:20},(_,i)=>i+82));
      assert(await page.locator('#historyBrowseBar').isHidden());
      await page.evaluate(() => { db.push({id:102,role:'user',content:'Live after returning'}); return pollNewMessages(); });
      assert((await ids(page)).includes(102));

      // Keyword/message-ID navigation still uses the same context window.
      await page.evaluate(() => jumpToMessage(40));
      assert.deepEqual(await ids(page), Array.from({length:11},(_,i)=>i+35));
      // Scrolling through the remaining pages resumes live mode at the actual end.
      await page.evaluate(async () => { while (historyHasMoreBelow) await loadMoreBelow(); });
      assert.equal(await page.evaluate(() => isBrowsingHistory), false);
      assert.deepEqual(await ids(page), Array.from({length:68},(_,i)=>i+35));
      assert.equal(errors.length, 0, errors.join('\n'));
      await page.close();
    }
    console.log('PASS: single/group date controls, context highlight, earlier/later pages, empty/error preservation, stale poll isolation, latest return and resumed polling');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
