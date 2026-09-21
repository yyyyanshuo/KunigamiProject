const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const source = fs.readFileSync(path.join(__dirname, '../templates/chat.html'), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a);
  return source.slice(a, b);
};
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage();
    await page.setContent('<div id="chatContainer"></div>');
    await page.addStyleTag({content: section('<style>', '</style>').slice(7)});
    await page.addScriptTag({content: `
      const chatContainer = document.getElementById('chatContainer');
      let isGroupMode = false;
      const currentConfig = {}, charConfig = {}, groupMembers = {}, currentUserName = 'User';
      const currentId = 'hero', aiLanguage = 'zh', copyIconPath = '', RECORD_TIMEZONE = 'Asia/Shanghai';
      const legacyGenerationErrorMessage = () => '';
      const isSystemPromptMessage = () => false;
      const extractCruiseFromContent = cleanText => ({cleanText, cruises: []});
      const extractTicklesFromContent = rest => ({rest, tickles: []});
      const extractForumBlocks = cleanText => ({cleanText, blocks: []});
      const extractChatlogBlocks = extractForumBlocks, extractThoughtsBlocks = extractForumBlocks;
      const parseChatTimestamp = timestamp => new Date(timestamp);
      const parseTransferMessageSegment = () => null;
      const stripImageTagsFromText = text => text;
      const bindCompactMessageActions = () => {};
      ${section('  function extractSafetyAlertFromContent(text)', '  function parseTransferMessageSegment(text)')}
      ${section('  function renderMessageGroup(', '    // --- 【新增】全局日期重排函数')}
    `});
    for (const groupMode of [false, true]) {
      for (const content of [
        '我在这里/[SAFETY_ALERT]',
        '[SAFETY_ALERT]/我在这里/[SAFETY_ALERT]',
        '我在这里/【SAFETY_ALERT：模型提示语】',
        '[SAFETY_ALERT]',
        '撤回内容/[recall]/[SAFETY_ALERT]',
      ]) {
        const result = await page.evaluate(({content, groupMode}) => {
          isGroupMode = groupMode;
          chatContainer.innerHTML = '';
          const msg = {id: 1, content, role: groupMode ? 'member' : 'assistant', timestamp: '2026-09-06T12:00:00Z'};
          let group = renderMessageGroup(msg);
          const first = group.querySelector('.safety-alert-row').textContent;
          const raw = group.dataset.originalContent;
          group.remove();
          group = renderMessageGroup({...msg, content: raw}, 'prepend');
          const row = group.querySelector('.safety-alert-row');
          return {
            count: group.querySelectorAll('.safety-alert-row').length,
            first, text: row.textContent, raw,
            body: group.innerText,
            bubbles: group.querySelectorAll('.bubble').length,
            align: getComputedStyle(row).textAlign,
            width: row.getBoundingClientRect().width,
            groupWidth: group.getBoundingClientRect().width,
          };
        }, {content, groupMode});
        assert.equal(result.count, 1);
        assert.equal(result.raw, content);
        assert.equal(result.first, result.text);
        assert(result.text.includes('12356'));
        assert(!result.body.includes('SAFETY_ALERT'));
        assert(!result.body.includes('模型提示语'));
        assert.equal(result.align, 'center');
        assert.equal(result.width, result.groupWidth);
        if (content === '[SAFETY_ALERT]') assert.equal(result.bubbles, 0);
      }
    }
    console.log('PASS: private/group real renderer, fixed centered notice, legacy/repeated/mid-message markers, marker-only/recall messages, raw content and reload');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
