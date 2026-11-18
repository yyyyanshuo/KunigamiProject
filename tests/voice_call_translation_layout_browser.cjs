// Exercise mobile touch hit testing with the real call page and synthetic call data.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(process.argv[2] || path.join(root, 'templates/call.html'), 'utf8')
  .replace(/{%[\s\S]*?%}/g, '').replace(/{{\s*call_id\|tojson\s*}}/g, '"touch-test"')
  .replace(/{{[\s\S]*?}}/g, 'zh-CN');
const turns = Array.from({length: 12}, (_, i) => ({
  id: String(i + 1), role: 'assistant', content: 'Souviens-toi de cette conversation. '.repeat(4) + i
}));

async function check(browser, width, height, coveredBottom = 0, reportsResize = false) {
  const page = await browser.newPage({viewport: {width, height}, isMobile: true, hasTouch: true});
  const translated = [];
  try {
    await page.addInitScript(() => {
      window.SakiI18n = {t: s => s, ui: (s, value) => s.join(value)};
      window.SakiCallSpeech = {CallSpeechInput: class {
        constructor(options) { this.options = options; }
        start() { this.options.onStatus('listening'); this.options.onText('长字幕'.repeat(100)); }
        close() {}
      }};
    });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/call/touch-test') return route.fulfill({contentType:'text/html', body:html});
      if (url.pathname === '/static/ai-disclosure.css') return route.fulfill({path:path.join(root, url.pathname), contentType:'text/css'});
      if (url.pathname === '/api/calls/touch-test') return route.fulfill({json:{call:{
        status:'active', tts_enabled:false, char_id:'test', character:{name:'Test', avatar:'/avatar.svg'}, turns
      }}});
      if (url.pathname === '/api/calls/touch-test/end') return route.fulfill({json:{call:{status:'ended', end_reason:'hangup'}}});
      if (url.pathname === '/api/translate') {
        translated.push(route.request().postDataJSON().text);
        return route.fulfill({json:{result:'记住这次通话。'}});
      }
      if (url.pathname === '/avatar.svg') return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg"/>'});
      return route.fulfill({contentType:'application/javascript', body:''});
    });
    await page.goto('https://layout.test/call/touch-test');
    await page.locator('#sendSpeechBtn:visible').waitFor();
    await page.evaluate(({coveredBottom, reportsResize}) => {
      if (coveredBottom) {
        const systemBar = document.createElement('div');
        systemBar.style.cssText = `position:fixed;inset:auto 0 0;height:${coveredBottom}px;z-index:999;background:#333`;
        document.body.appendChild(systemBar);
      }
      if (reportsResize) {
        Object.defineProperty(window.visualViewport, 'height', {get: () => innerHeight - coveredBottom});
        window.visualViewport.dispatchEvent(new Event('resize'));
      }
      document.querySelector('#playGate').style.display = 'block';
    }, {coveredBottom, reportsResize});
    const clipped = await page.evaluate(() => {
      const controls = document.querySelectorAll('#activeActions button, #activeActions .action-labels span');
      return [...controls].filter(el => {
        const r = el.getBoundingClientRect();
        return ![r.top + 2, r.bottom - 2].every(y => el.contains(document.elementFromPoint(r.x + r.width / 2, y)));
      }).map(el => el.id || el.textContent);
    });
    assert.deepEqual(clipped, [], 'Both complete controls and their labels must be visible without scrolling');
    const bounds = await page.evaluate(() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect();
      return {overlap:rect('.transcript').bottom - rect('.bottom').top, transcriptHeight:rect('.transcript').height};
    });
    assert(bounds.overlap <= 1, `Controls overlap transcript by ${bounds.overlap}px at ${width}×${height}`);
    assert(bounds.transcriptHeight >= 44);
    // Tap at the physical screen coordinates so a transparent overlay fails the test.
    for (const index of [turns.length - 1, 2]) {
      const turn = page.locator('.turn.assistant').nth(index);
      const button = turn.locator('button');
      await button.scrollIntoViewIfNeeded();
      const size = await button.boundingBox();
      assert(size.height <= 24 && size.width <= 32, 'Keep the visible translation pill compact');
      const center = await button.evaluate(el => {
        const {x, y, width, height} = el.getBoundingClientRect();
        return {x:x + width / 2, y:y + height / 2};
      });
      // The small visible pill still accepts a tap just outside its painted edge.
      await page.touchscreen.tap(center.x - size.width / 2 - 3, center.y);
      await turn.locator('.turn-translation[data-loaded=true]:visible').waitFor();
      assert.equal(translated.at(-1), turns[index].content);
      const canCollapse = await button.evaluate(el => {
        const rect = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
      });
      assert(canCollapse, 'The translated turn and its collapse button must stay in view');
      await button.tap();
      assert(await turn.locator('.turn-translation').isHidden());
    }
    await page.locator('#muteBtn').tap();
    assert.equal(await page.locator('#muteLabel').innerText(), '取消静音');
    await page.locator('#hangupBtn').tap();
    await page.locator('#backBtn:visible').waitFor();
    assert.equal(await page.locator('#status').innerText(), '通话已结束');
    console.log(`PASS ${width}×${height}, bottom cover ${coveredBottom}px: translation, collapse, complete controls, mute and hangup`);
  } finally { await page.close(); }
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const args of [[372,818,52], [360,640], [390,844,58,true], [667,375,54,true]]) {
      await check(browser, ...args);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
