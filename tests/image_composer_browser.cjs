/* Real DOM/editor + the production persistence/generation functions; all services are fixtures. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const fixtureImage = fs.readFileSync(path.join(root, 'static/logo.png')).toString('base64');
const source = fs.readFileSync(path.join(root, 'templates/chat.html'), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, start); return source.slice(a, b);
};
const modal = section('<div id="imageDescriptionModal"', '<!-- 【新增】底部多选操作栏 -->')
  .replace(/\{\{\s*ui_t\('([^']*)'\)\s*\}\}/g, '$1');
const css = Array.from(source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g), m => m[1]).join('\n') + '\n' + fs.readFileSync(path.join(root, 'static/composer-surfaces.css'), 'utf8');
const translationRow = section('<div id="translation-input-container"', '<div id="voiceLivePanel"');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    for (const groupMode of [false, true]) {
      const page = await browser.newPage({viewport: {width: 390, height: 844}});
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      let acceptDraft = false, confirmationCount = 0;
      page.on('dialog', async dialog => {confirmationCount++; if (acceptDraft) await dialog.accept(); else await dialog.dismiss();});
      await page.route('**/*', route => route.fulfill({contentType: 'text/html; charset=utf-8', body:
        `<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><style>${css}</style>${translationRow}<textarea id="messageInput"></textarea><button id="sendBtn">发送</button><div id="chatContainer"></div>${modal}</html>`}));
      await page.goto('http://saki.test/');
      for (const name of ['ui-language.js', 'image-tags.js', 'message-tags.js', 'image-composer.js']) {
        await page.addScriptTag({path: path.join(root, 'static', name)});
      }
      await page.addScriptTag({content: `
        const input = document.getElementById('messageInput'), btn = document.getElementById('sendBtn');
        const chatContainer = document.getElementById('chatContainer');
        const isGroupMode = ${groupMode}, currentId = 'testgroup', charId = 'testchar';
        const agentIsWaiting = false;
        let pendingUserTransSource = null, isLoading = false, isBrowsingHistory = false, historyViewVersion = 0, aiLanguage = 'ja';
        window.uploads = []; window.requests = []; window.saved = [];
        window.translations = []; window.sources = {}; window.transResult = null;
        window.transFailure = false; window.delayTranslation = false;
        window.failVision = false; window.failPersist = false; window.failGeneration = false; window.delayUpload = false;
        const pendingSentTranslations = new Map(), chatHiddenTrans = new Set();
        const _chatTransSet = (key, value) => { sources[key] = value; };
        const _getChatSourceCacheKey = id => id;
        const _chatTransToggleKey = id => id;
        const stripRubyTags = text => text;
        const sendMessage = () => { throw new Error('Unexpected automatic send'); };
        ${section('  function _applyPendingTranslation()', '  function _getChatTransCacheKey(')}
        const pollNewMessages = async () => {};
        const generationErrorPayload = error => ({message:error.message});
        function renderGenerationErrorGroup(payload, id, content) {
          const group = document.createElement('div'); group.id = 'retry';
          group.dataset.retryable = 'true'; group.dataset.retryUserMessageId = id; group.dataset.retryUserContent = content;
          chatContainer.append(group);
        }
        function configureGenerationErrorGroup() {}
        function renderMessageGroup() {throw new Error('Unexpected placeholder bubble');}
        window.fetch = async (url, options) => {
          if (url !== '/api/translate') throw new Error('Unexpected translation route '+url);
          const payload = JSON.parse(options.body); translations.push(payload);
          if (delayTranslation) await new Promise(resolve => window.releaseTranslation = resolve);
          if (transFailure) return new Response('<html>Timeout</html>', {status:504});
          return Response.json({result: transResult === null ? payload.text.replace(/今天/g,'Today').replace(/给你看看/g,'Take a look').replace(/怎么样？/g,'What do you think?') : transResult});
        };
        async function fetchWithTimeout(url, options = {}) {
          if (url.endsWith('/preferences')) throw new Error('Unexpected removed preferences request');
          if (url.endsWith('/upload')) {
            uploads.push({mode:options.body.get('description_mode'), prompt:options.body.get('vision_prompt'), description:options.body.get('description')});
            if (delayUpload) await new Promise(resolve => window.releaseUpload = resolve);
            if (failVision) return {ok:false,json:async()=>({code:'vision_failed',error:'AI识图失败，请改用手动描述'})};
            return {ok:true,json:async()=>({status:'success',path:'fixture.jpg',description:options.body.get('description') || 'hidden image description / f(x/y)'})};
          }
          const payload = JSON.parse(options.body); requests.push({url,payload});
          if (url.endsWith('/messages')) {
            if (failPersist) return {ok:false,json:async()=>({error:'保存失败'})};
            saved.push(payload.message);
            return {ok:true,json:async()=>({user_id:saved.length})};
          }
          return failGeneration ? {ok:false,json:async()=>({error:'model unavailable'})} : {ok:true,json:async()=>({})};
        }
        ${section('  function splitForwardedChatlogSegments(text)', '  // 不使用任意 innerHTML')}
        ${section('    async function sendChatContent(', '    // ==============================')}
        ${section('    async function retryGenerationError(', '    async function sendTickle(')}
        ${section('    function autoResizeTextarea(', '    // --- 新增逻辑函数 ---')}
        ${section('    const imageComposer = SakiImageComposer.create(', '    const transferEditorOptions =')}
        window.openFixture = async () => {
          const bytes = Uint8Array.from(atob('${fixtureImage}'), c=>c.charCodeAt(0));
          await imageComposer.open(new File([bytes], 'test.png', {type:'image/png'}));
        };
      `});
      await page.locator('#messageInput').fill('今天/给你看看[图片]怎么样？');
      await page.evaluate(() => openFixture());
      assert.equal(await page.locator('#imageComposerTitle').innerText(), '发送图片');
      assert.equal(await page.locator('#imageVisionPrompt').inputValue(), '');
      assert.equal(await page.locator('#imageCaption').inputValue(), '今天/给你看看[图片]怎么样？');
      assert(await page.locator('#imageCaptionTranslationRow').isVisible());
      assert.match(await page.locator('#imageCaptionTranslationInput').getAttribute('placeholder'), /日本語/);
      const matchingStyle = await page.evaluate(() => {
        const normal=getComputedStyle(document.getElementById('transInput'));
        const image=getComputedStyle(document.getElementById('imageCaptionTranslationInput'));
        return ['fontSize','lineHeight','minHeight','borderRadius','borderColor','padding'].every(key=>normal[key]===image[key]);
      });
      assert(matchingStyle, 'Both translation inputs share the same styles');
      assert.equal(await page.locator('#imageMessagePreview > div').count(), 4);
      assert.equal(await page.locator('#imageMessagePreview img').count(), 1);
      assert(!(await page.locator('#imageMessagePreview').innerText()).includes('preview.jpg'));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);

      assert.equal(await page.locator('#imageDescriptionModal select, #imageDescriptionModal input[type="file"]').count(), 0);
      assert.equal(await page.locator('#imagePreviewSection').getAttribute('open'), null);
      await page.locator('#imagePreviewSection summary').click();
      assert(await page.locator('#imageMessagePreview img').isVisible());
      await page.locator('#imagePreviewSection summary').press('Space');
      assert.equal(await page.locator('#imagePreviewSection').getAttribute('open'), null);

      // Inspect compact, narrow-mobile and desktop layouts with the production CSS.
      if (!groupMode) {
        await page.locator('#imageVisionPrompt').fill('重点描述人物的衣着、表情和动作，用三句话总结。');
        const shots = process.env.SAKI_IMAGE_SCREENSHOT_DIR;
        if (shots) fs.mkdirSync(shots, {recursive:true});
        for (const viewport of [{width:390,height:844}, {width:320,height:568}, {width:1280,height:1000}]) {
          await page.setViewportSize(viewport);
          for (const expanded of [false, true]) {
            await page.evaluate(value => {
              document.getElementById('imagePreviewSection').open = value;
              document.querySelector('.image-composer-body').scrollTop = value ? 10000 : 0;
            }, expanded);
            const layout = await page.evaluate(() => {
              const rect = selector => document.querySelector(selector).getBoundingClientRect();
              const caption = rect('.image-composer-caption'), prompt = rect('#imagePromptPanel');
              const preview = rect('#imagePreviewSection'), footer = rect('.image-composer-footer');
              const send = rect('#imageDescriptionConfirm'), dialog = rect('.image-description-dialog');
              return {fieldGap:caption.top-prompt.bottom, previewGap:preview.top-caption.bottom,
                fits:dialog.left>=0 && dialog.right<=innerWidth && dialog.top>=0 && dialog.bottom<=innerHeight,
                sendVisible:send.bottom<=innerHeight && send.top>=footer.top,
                noOverflow:document.querySelector('.image-composer-body').scrollWidth<=document.querySelector('.image-composer-body').clientWidth,
                padding:parseFloat(getComputedStyle(document.querySelector('#imagePreviewSection summary')).paddingTop)};
            });
            assert(layout.fieldGap >= 20 && layout.previewGap >= 20, JSON.stringify(layout));
            assert(layout.fits && layout.sendVisible && layout.noOverflow, JSON.stringify(layout));
            assert(layout.padding >= 14);
            if (shots) await page.screenshot({path:path.join(shots, `image-caption-translation-${viewport.width}-${expanded?'expanded':'collapsed'}.png`)});
          }
        }
        await page.setViewportSize({width:390,height:844});
      }
      // Cancel keeps the original main draft; new images start with blank instructions.
      await page.locator('#imageVisionPrompt').fill('本次临时要求');
      await page.locator('#imageDescriptionCancel').click();
      assert.equal(await page.locator('#messageInput').inputValue(), '今天/给你看看[图片]怎么样？');
      await page.evaluate(() => openFixture());
      assert.equal(await page.locator('#imageVisionPrompt').inputValue(), '');
      assert.equal(await page.locator('#imagePreviewSection').getAttribute('open'), null);
      await page.locator('#imageVisionPrompt').fill('重点描述服装');

      // Duplicate positions fail before upload; an AI failure preserves fields and offers manual mode.
      await page.locator('#imageCaption').fill('[图片]/[IMAGE]');
      await page.locator('#imageDescriptionConfirm').click();
      assert.equal(await page.evaluate(() => uploads.length), 0);
      assert.match(await page.locator('#imageComposerStatus').innerText(), /只能指定一个位置/);
      await page.locator('#imageCaption').fill('开头[图片]结尾');
      await page.evaluate(() => { failVision = true; });
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(() => !document.getElementById('imageManualFallback').hidden);
      assert.equal(await page.locator('#imageCaption').inputValue(), '开头[图片]结尾');
      assert.equal(await page.locator('#imageVisionPrompt').inputValue(), '重点描述服装');
      await page.locator('#imageManualFallback').click();
      await page.locator('#imageDescriptionInput').fill('手动内容 / f(x/y)');
      await page.evaluate(() => { failVision = false; failPersist = true; });
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(() => document.getElementById('imageComposerStatus').textContent === '保存失败');
      assert.equal(await page.evaluate(() => uploads.at(-1).mode), 'manual');
      assert.equal(await page.evaluate(() => uploads.at(-1).prompt), null);
      assert.equal(await page.locator('#imageCaption').inputValue(), '开头[图片]结尾');
      assert.notEqual(await page.locator('#messageInput').inputValue(), '');

      // Retry persistence uses the uploaded result. Retry generation uses the same saved message ID.
      await page.evaluate(() => { failPersist = false; failGeneration = true; });
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(() => saved.length === 1 && document.getElementById('retry'));
      assert.equal(await page.evaluate(() => uploads.length), 2);
      assert.equal(await page.locator('#messageInput').inputValue(), '');
      assert.equal(await page.locator('#imageDescriptionModal').isVisible(), false);
      assert.equal(await page.evaluate(() => saved[0]), '开头 / [IMAGE](fixture.jpg)(手动内容 / f(x/y)) / 结尾');
      assert.equal(await page.evaluate(() => requests.at(-1).payload.user_message_id), 1);
      assert.match(await page.evaluate(() => requests.at(-1).url), groupMode ? /\/group\/testgroup\/chat$/ : /\/testchar\/chat_v2$/);
      await page.evaluate(async () => { failGeneration = false; await retryGenerationError(document.getElementById('retry')); });
      assert.equal(await page.evaluate(() => saved.length), 1);
      assert.equal(await page.evaluate(() => uploads.length), 2);
      assert.equal(await page.evaluate(() => requests.at(-1).payload.user_message_id), '1');

      // Double submit while upload is pending cannot create duplicate requests or messages.
      await page.locator('#messageInput').fill('https://example.com/a/b[图片]后');
      await page.evaluate(() => openFixture());
      assert.equal(await page.locator('#imageMessagePreview > div').count(), 3);
      await page.locator('#imageVisionPrompt').fill('只给识图模型的要求');
      await page.evaluate(() => { delayUpload = true; });
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(() => !!window.releaseUpload);
      await page.evaluate(() => document.getElementById('imageDescriptionConfirm').click());
      assert.equal(await page.evaluate(() => uploads.length), 3);
      await page.evaluate(() => { releaseUpload(); });
      await page.waitForFunction(() => saved.length === 2 && !document.getElementById('sendBtn').disabled);
      assert(!await page.evaluate(() => saved.at(-1).includes('只给识图模型')));
      assert.equal(await page.evaluate(() => uploads.at(-1).prompt), '只给识图模型的要求');

      // Same-language targets hide the row; the resolved chat target is read on opening.
      await page.evaluate(() => {delayUpload=false;aiLanguage='zh-CN';});
      await page.evaluate(() => openFixture());
      assert(await page.locator('#imageCaptionTranslationRow').isHidden());
      await page.locator('#imageDescriptionCancel').click();
      await page.evaluate(() => {aiLanguage='English';});
      await page.evaluate(() => openFixture());
      assert(await page.locator('#imageCaptionTranslationRow').isVisible());
      assert.match(await page.locator('#imageCaptionTranslationInput').getAttribute('placeholder'), /English/);
      await page.locator('#imageDescriptionCancel').click();

      // A normal translated draft carries its source through the image send path.
      await page.evaluate(() => {aiLanguage='ja';document.getElementById('translation-input-container').style.display='flex';});
      await page.locator('#transInput').fill('今天');
      await page.locator('#transInput').press('Enter');
      await page.waitForFunction(()=>!document.getElementById('transBtn').disabled);
      assert.equal(await page.locator('#messageInput').inputValue(),'Today');
      assert.equal(await page.evaluate(()=>pendingUserTransSource),'今天');
      await page.evaluate(() => openFixture());
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(()=>saved.length===3 && !document.getElementById('sendBtn').disabled);
      assert.equal(await page.evaluate(()=>sources[3]),'今天');

      // The image translator uses the same request/context while keeping both main drafts intact.
      await page.locator('#messageInput').fill('主聊天草稿');
      await page.locator('#transInput').fill('主聊天翻译草稿');
      await page.evaluate(()=>{
        const group=document.createElement('div');group.className='message-group';
        group.dataset.originalContent='刚刚聊到穿搭';chatContainer.append(group);
      });
      await page.evaluate(() => openFixture());
      const imageSource='今天/给你看看[图片]怎么样？';
      await page.locator('#imageCaptionTranslationInput').fill(imageSource);
      const beforeConfirm=confirmationCount;
      await page.locator('#imageCaptionTranslationButton').click();
      await page.waitForFunction(()=>!document.getElementById('imageCaptionTranslationButton').disabled);
      assert.equal(confirmationCount,beforeConfirm+1);
      assert.equal(await page.locator('#imageCaption').inputValue(),'主聊天草稿');
      assert.equal(await page.locator('#imageCaptionTranslationInput').inputValue(),imageSource);
      acceptDraft=true;
      await page.locator('#imageCaptionTranslationInput').press('Enter');
      await page.waitForFunction(()=>!document.getElementById('imageCaptionTranslationButton').disabled);
      assert.equal(await page.locator('#imageCaption').inputValue(),'Today/Take a look[图片]What do you think?');
      assert.equal(await page.locator('#imageCaptionTranslationInput').inputValue(),'');
      assert.equal(await page.locator('#imageMessagePreview > div').count(),4);
      assert.equal(await page.locator('#messageInput').inputValue(),'主聊天草稿');
      assert.equal(await page.locator('#transInput').inputValue(),'主聊天翻译草稿');
      const request=await page.evaluate(()=>translations.at(-1));
      assert.equal(request.purpose,'send');assert.equal(request.scene,groupMode?'group':'single');
      assert.equal(request[groupMode?'group_id':'char_id'],'testgroup');
      assert(request.context.includes('刚刚聊到穿搭'));
      assert.equal(request.protected_tokens.length,2);
      assert(!request.text.includes('[图片]') && !request.text.includes('/'));
      assert.equal(await page.evaluate(()=>saved.length),3);

      // Save failure retains the translated draft/source and reuses the uploaded image.
      await page.evaluate(()=>{failPersist=true;});
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(()=>document.getElementById('imageComposerStatus').textContent==='保存失败');
      const uploaded=await page.evaluate(()=>uploads.length);
      await page.evaluate(()=>{failPersist=false;});
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(()=>saved.length===4 && !document.getElementById('sendBtn').disabled);
      assert.equal(await page.evaluate(()=>uploads.length),uploaded);
      assert.equal(await page.evaluate(()=>sources[4]),imageSource);
      // Exercise the production source-note renderer against the persisted message ID.
      await page.evaluate(()=>{
        const group=document.createElement('div');group.className='message-group user';group.dataset.id='4';
        const wrapper=document.createElement('div');wrapper.className='message-wrapper';group.append(wrapper);
        chatContainer.append(group);_applyPendingTranslation();
      });
      assert.equal(await page.locator('[data-id="4"] .translation-source').innerText(),imageSource);

      // Keyboard composition/newlines and retryable translation errors preserve the caption.
      await page.evaluate(() => openFixture());
      await page.locator('#imageCaption').fill('保留我的配文');
      await page.locator('#imageCaptionTranslationInput').fill(imageSource);
      const translationCount=await page.evaluate(()=>translations.length);
      await page.locator('#imageCaptionTranslationInput').press('Shift+Enter');
      await page.locator('#imageCaptionTranslationInput').dispatchEvent('keydown',{key:'Enter',isComposing:true});
      assert.equal(await page.evaluate(()=>translations.length),translationCount);
      await page.evaluate(()=>{transFailure=true;});
      await page.locator('#imageCaptionTranslationButton').click();
      await page.waitForFunction(()=>!document.getElementById('imageCaptionTranslationButton').disabled);
      assert.match(await page.locator('#imageComposerStatus').innerText(),/HTTP 504/);
      assert.equal(await page.locator('#imageCaption').inputValue(),'保留我的配文');
      assert((await page.locator('#imageCaptionTranslationInput').inputValue()).includes(imageSource));
      await page.evaluate(()=>{transFailure=false;transResult='图片位置丢了';});
      await page.locator('#imageCaptionTranslationButton').click();
      await page.waitForFunction(()=>!document.getElementById('imageCaptionTranslationButton').disabled);
      assert.match(await page.locator('#imageComposerStatus').innerText(),/图片位置或分隔符/);
      assert.equal(await page.locator('#imageCaption').inputValue(),'保留我的配文');

      await page.evaluate(()=>{transResult=null;delayTranslation=true;releaseTranslation=null;});
      await page.locator('#imageCaptionTranslationButton').click();
      await page.waitForFunction(()=>Boolean(window.releaseTranslation));
      await page.locator('#imageCaption').fill('等待时改写的配文');
      await page.locator('#imageCaptionTranslationInput').fill('下一句原文');
      acceptDraft=false;
      await page.evaluate(()=>releaseTranslation());
      await page.waitForFunction(()=>!document.getElementById('imageCaptionTranslationButton').disabled);
      assert.equal(await page.locator('#imageCaption').inputValue(),'等待时改写的配文');
      assert.equal(await page.locator('#imageCaptionTranslationInput').inputValue(),'下一句原文');
      acceptDraft=true;

      // Repeated clicks and Send are guarded while translation is pending. Closing
      // still works even if a provider ignores cancellation and responds late.
      await page.evaluate(()=>{transResult=null;delayTranslation=true;releaseTranslation=null;});
      await page.locator('#imageCaptionTranslationButton').click();
      await page.waitForFunction(()=>Boolean(window.releaseTranslation));
      const pendingCount=await page.evaluate(()=>translations.length);
      await page.evaluate(()=>{document.getElementById('imageCaptionTranslationButton').click();document.getElementById('imageDescriptionConfirm').click();});
      assert.equal(await page.evaluate(()=>translations.length),pendingCount);
      assert.equal(await page.evaluate(()=>saved.length),4);
      assert(await page.locator('#imageDescriptionCancel').isEnabled());
      await page.keyboard.press('Escape');
      assert(await page.locator('#imageDescriptionModal').isHidden());
      await page.evaluate(() => openFixture());
      await page.locator('#imageCaption').fill('新图片的配文');
      const confirmations=confirmationCount;
      await page.evaluate(async()=>{releaseTranslation();await new Promise(resolve=>setTimeout(resolve,50));});
      assert.equal(await page.locator('#imageCaption').inputValue(),'新图片的配文');
      assert.equal(confirmationCount,confirmations);
      assert(await page.locator('#imageCaptionTranslationButton').isEnabled());

      // Manual edits retire the source association; manual image descriptions
      // support translation just like AI recognition does.
      await page.evaluate(()=>{delayTranslation=false;});
      await page.locator('.image-description-mode[data-mode="manual"]').click();
      await page.locator('#imageDescriptionInput').fill('一张照片');
      await page.locator('#imageCaptionTranslationInput').fill('今天');
      await page.locator('#imageCaptionTranslationButton').click();
      await page.waitForFunction(()=>!document.getElementById('imageCaptionTranslationButton').disabled);
      await page.locator('#imageCaption').fill('自己改写的译文');
      await page.locator('#imageDescriptionConfirm').click();
      await page.waitForFunction(()=>saved.length===5 && !document.getElementById('sendBtn').disabled);
      assert.equal(await page.evaluate(()=>sources[5]),undefined);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS: single/group image translation, shared styles and language rules, protected positions, keyboard, draft confirmation/isolation, errors/cancellation, source notes, manual edits, AI/manual modes, mobile/desktop layout and send retries');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
