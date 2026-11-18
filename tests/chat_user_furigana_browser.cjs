const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'templates/chat.html'), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a);
  return source.slice(a, b);
};

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    for (const enabled of [true, false]) {
      const page = await browser.newPage();
      await page.route('**/*', route => route.abort());
      await page.setContent('<html lang="en"><body><div id="chatContainer"></div></body></html>');
      await page.evaluate(enabled => {
        window.SakiFuriganaEnabled = enabled;
        window.requests = [];
        window.fetch = async (url, options) => {
          assertFuriganaUrl(url);
          const {text} = JSON.parse(options.body);
          requests.push(text);
          await new Promise(resolve => setTimeout(resolve, 30));
          return {ok: true, json: async () => ({result: text.replaceAll('学校', '<ruby>学校<rt>がっこう</rt></ruby>')})};
        };
        function assertFuriganaUrl(url) { if (url !== '/api/furigana') throw new Error('Unexpected request: ' + url); }
      }, enabled);
      for (const file of ['ui-language.js', 'image-tags.js', 'message-tags.js', 'safe-chat-renderer.js', 'model-furigana.js']) {
        await page.addScriptTag({path: path.join(root, 'static', file)});
      }
      await page.addScriptTag({content: `
        const chatContainer = document.getElementById('chatContainer');
        let isGroupMode = false;
        const currentConfig = {}, charConfig = {}, groupMembers = {}, currentUserName = 'User';
        const currentId = 'demo', aiLanguage = 'ja', copyIconPath = '', RECORD_TIMEZONE = 'Asia/Shanghai';
        const extractCruiseFromContent = cleanText => ({cleanText, cruises: []});
        const extractTicklesFromContent = rest => ({rest, tickles: []});
        const extractForumBlocks = cleanText => ({cleanText, blocks: []});
        const extractChatlogBlocks = extractForumBlocks, extractThoughtsBlocks = extractForumBlocks;
        const parseChatTimestamp = timestamp => new Date(timestamp);
        const parseTransferMessageSegment = () => null;
        const bindCompactMessageActions = () => {};
        const refreshDateSeparators = () => {};
        ${section('  function needsReadingTranslation()', '  function legacyGenerationErrorMessage(text)')}
        const legacyGenerationErrorMessage = () => '';
        ${section('  function stripRubyTags(text)', '  async function openEditModal(')}
        ${section('  function extractSafetyAlertFromContent(text)', '  function parseTransferMessageSegment(text)')}
        ${section('  function replaceRenderedMessageGroup(message)', '  async function refreshRenderedMessageById(')}
        ${section('  function renderSystemPromptGroup(', '    // --- 【新增】全局日期重排函数')}
      `});
      for (const groupMode of [false, true]) {
        for (const mode of ['append', 'prepend']) {
          await page.evaluate(({groupMode, mode}) => {
            isGroupMode = groupMode;
            chatContainer.innerHTML = '';
            renderMessageGroup({id: 42, role: 'user', content: '今日は学校です', timestamp: '2026-09-29T12:00:00Z'}, mode);
          }, {groupMode, mode});
          if (enabled) await page.waitForFunction(() => document.querySelector('.user .bubble ruby'));
          else await page.waitForTimeout(250);
          assert.equal(await page.locator('.user .bubble ruby').count(), enabled ? 1 : 0);
          assert.equal(await page.locator('.message-group').getAttribute('data-original-content'), '今日は学校です');
          await page.evaluate(() => replaceRenderedMessageGroup({id: 42, content: '明日は学校です'}));
          if (enabled) await page.waitForFunction(() => document.querySelector('.user .bubble ruby'));
          else await page.waitForTimeout(250);
          assert.equal(await page.locator('.user .bubble ruby').count(), enabled ? 1 : 0);
          assert.equal(await page.locator('.message-group').getAttribute('data-original-content'), '明日は学校です');
          assert.equal(await page.locator('button ruby, ruby ruby').count(), 0);
        }
      }
      if (enabled) {
        await page.evaluate(() => {
          chatContainer.innerHTML = '';
          renderMessageGroup({id: 43, role: 'user', content: '今日は<ruby>学校<rt>がっこう</rt></ruby>です', timestamp: '2026-09-29T12:00:00Z'}, 'prepend');
          renderMessageGroup({id: 44, role: 'user', content: '（システム：今日は学校です）', timestamp: '2026-09-29T12:00:00Z'});
          renderMessageGroup({id: 45, role: 'user', content: '[voice_message](sample.webm)(今日は学校です)', timestamp: '2026-09-29T12:00:00Z'});
        });
        await page.waitForFunction(() => document.querySelector('[data-id="45"] [data-furigana-output] ruby'));
        assert.equal(await page.locator('[data-id="43"] ruby').count(), 1);
        assert.equal(await page.locator('[data-id="44"] ruby, ruby ruby').count(), 0);
        assert.equal(await page.locator('[data-id="45"]').getAttribute('data-original-content'), '[voice_message](sample.webm)(今日は学校です)');
        assert.equal(await page.evaluate(() => SakiModelDisplay.plain(document.querySelector('[data-id="43"] .bubble'))), '今日は学校です');
      } else assert.equal(await page.evaluate(() => requests.length), 0);
      await page.close();
    }
    console.log('PASS: single/group user readings after append, history reconstruction and edit; disabled preference, system notices, existing ruby and raw source preserved');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
