/* Production Moments template/JS with local fixtures; no live account or model calls. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
let html = fs.readFileSync(path.join(root, 'templates/moments.html'), 'utf8')
  .replace(/\{%[\s\S]*?%\}/g, '')
  .replace(/\{\{\s*user_timezone[^}]*\}\}/g, '"Asia/Shanghai"')
  .replace(/\{\{\s*ui_language\s*\}\}/g, 'zh-CN')
  .replace(/\{\{[\s\S]*?\}\}/g, '')
  .replace(/<script[^>]+src="[^"]*(?:auth-guard|ai-disclosure|ai-cruise|theme-loader|voice-call-incoming)[^"]*"[^>]*><\/script>/g, '')
  .replace('<head>', '<head><style>#theme-loading-screen {display:none!important;}</style><script src="/static/ui-language.js"></script><script src="/static/ai-disclosure.js"></script>');
const characters = [{id:'blocked',name:'角色甲',avatar:'/static/logo.png'},
  {id:'allowed',name:'角色乙',avatar:'/static/default_avatar.png'},
  ...Array.from({length:12},(_,i)=>({id:`character_${i}`,name:`角色 ${i+3}`,avatar:'/static/default_avatar.png'}))];
const makePost = (id, blocked) => ({char_id:id,timestamp:'2026-10-19 10:00:00',remark:id==='user'?'我':'角色乙',
  content:'今天想偷偷休息一下。',avatar:'/static/logo.png',liker_ids:[],liker_remarks:[],comments:[],
  blocked_character_ids:blocked,blocked_characters:characters.filter(row=>blocked.includes(row.id))});
(async()=>{
  const browser=await chromium.launch({headless:true, executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    const page=await browser.newPage({viewport:{width:390,height:844}});
    const errors=[],alerts=[],posts=[makePost('allowed',['blocked'])],requests=[];
    let failPicker=false, failPublish=false;
    await page.addInitScript(() => {
      window.copiedTexts = [];
      window.failClipboard = false;
      Object.defineProperty(navigator, 'clipboard', {value: {
        writeText: async text => {
          if (window.failClipboard) throw new Error('Clipboard unavailable');
          window.copiedTexts.push(text);
        }
      }});
      document.execCommand = command => {
        if (command === 'copy') window.copiedTexts.push(document.activeElement.value);
        return true;
      };
    });
    page.on('pageerror',e=>errors.push(e.message));
    page.on('dialog',async d=>{alerts.push(d.message());await d.dismiss();});
    await page.route('**/*',async route=>{
      const url=new URL(route.request().url()), pathname=url.pathname;
      const json=value=>route.fulfill({contentType:'application/json',body:JSON.stringify(value)});
      if(pathname==='/moments') return route.fulfill({contentType:'text/html; charset=utf-8',body:html});
      if(pathname.startsWith('/static/')) {
        const file=path.join(root,pathname);
        if(fs.existsSync(file)) return route.fulfill({path:file});
        return route.fulfill({status:404,body:''});
      }
      if(pathname==='/api/vision/prompts') return json({prompts:[]});
      if(pathname==='/api/user/profile_settings') return json({ai_language:'zh'});
      if(pathname==='/api/moments/active_enabled') return json({enabled:false});
      if(pathname==='/api/moments/characters') {
        if(url.searchParams.has('details')) return failPicker?route.fulfill({status:500,body:'error'}):json(characters);
        return json(Object.fromEntries(characters.map(row=>[row.id,row.name])));
      }
      if(pathname==='/api/moments/related_characters') return json({blocked:'角色甲',allowed:'角色乙'});
      if(pathname==='/api/moments') return json(posts);
      if(pathname==='/api/moments/post') {
        requests.push(route.request().postData());
        if(failPublish) return route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({error:'模拟发布失败'})});
        const body=route.request().postData();
        const blocked=JSON.parse(body.match(/name="blocked_character_ids"\r\n\r\n([^\r]+)/)[1]);
        posts.unshift(makePost('user',blocked));return json({status:'success',post:posts[0]});
      }
      if(pathname==='/api/moments/post/ai_comment') {requests.push(JSON.parse(route.request().postData()));return json({status:'success'});}
      throw new Error('Unexpected fixture request '+url);
    });
    await page.goto('http://saki.test/moments');
    const shots=process.env.SAKI_IMAGE_SCREENSHOT_DIR;
    if(shots) fs.mkdirSync(shots,{recursive:true});
    await page.locator('.moment-visibility-badge').waitFor();
    async function copyFirstPost() {
      const count = await page.evaluate(() => window.copiedTexts.length);
      await page.locator('.moment-head .moment-menu-btn').first().click();
      await page.locator('.btn-copy-moment').first().click();
      await page.waitForFunction(count => window.copiedTexts.length > count, count);
      return page.evaluate(() => window.copiedTexts.at(-1));
    }
    const copied = await copyFirstPost();
    assert(copied.includes('【谁不可见】角色甲\n'));
    assert(copied.includes('今天想偷偷休息一下。'));
    assert(copied.endsWith('——\nGenerated by Saki AI\nhttp://saki.test'));
    for(const viewport of [{width:320,height:568},{width:390,height:844},{width:1280,height:900}]) {
      await page.setViewportSize(viewport);
      const badge=await page.locator('.moment-footer .moment-visibility-badge').boundingBox();
      const disclosure=await page.locator('.moment-footer .moment-ai-disclosure').boundingBox();
      assert(badge && disclosure);
      assert(badge.x+badge.width<=disclosure.x, 'Visibility belongs left of AI disclosure');
      assert(Math.abs(badge.y+badge.height/2-disclosure.y-disclosure.height/2)<1, 'Footer labels align on the same row');
      if(shots) await page.screenshot({path:path.join(shots,`moment-hidden-footer-${viewport.width}.png`)});
    }
    await page.setViewportSize({width:390,height:844});
    await page.locator('.moment-visibility-badge').click();
    assert.equal(await page.locator('#momentVisibilityTitle').innerText(),'部分用户不可见');
    assert.equal(await page.locator('#momentVisibilityList input').count(),0);
    assert.equal(await page.locator('#momentVisibilityList strong').innerText(),'角色甲');
    assert.equal(await page.locator('#momentVisibilityApply').isVisible(),false);
    if(shots) await page.screenshot({path:path.join(shots,'moment-hidden-list-mobile.png')});
    await page.locator('#momentVisibilityCancel').click();
    // Disabled selection also has a JS guard against a stale/forged value.
    await page.locator('.moment-head .moment-menu-btn').first().click();
    await page.locator('.post-reply-ai-btn').first().click();
    await page.waitForFunction(()=>document.querySelector('#replyCharacterSelect option[value="blocked"]')?.disabled);
    await page.evaluate(()=>{document.getElementById('replyCharacterSelect').value='blocked';document.getElementById('replyModalSubmit').click();});
    assert(alerts.pop().includes('不可见'));
    assert.equal(requests.length,0);
    await page.locator('#replyCharacterSelect').selectOption('allowed');
    await page.locator('#replyModalSubmit').click();
    await page.waitForFunction(()=>getComputedStyle(document.getElementById('replyModalMask')).display==='none');
    assert.equal(requests.pop().commenter_id,'allowed');
    await page.locator('#openMomentModal').click();
    await page.locator('#momentComposeText').fill('这条只给部分角色看');
    await page.locator('#momentVisibilityOpen').click();
    await page.locator('#momentVisibilityList input[value="blocked"]').check();
    await page.locator('#momentVisibilityList input[value="allowed"]').check();
    for(const viewport of [{width:390,height:844},{width:320,height:568},{width:1280,height:900}]) {
      await page.setViewportSize(viewport);
      const box=await page.locator('#momentVisibilityApply').boundingBox();
      assert(box && box.y>=0 && box.y+box.height<=viewport.height);
      assert(await page.evaluate(()=>document.querySelector('.moment-visibility-dialog').scrollWidth<=document.querySelector('.moment-visibility-dialog').clientWidth));
      if(shots) await page.screenshot({path:path.join(shots,`moment-hidden-picker-${viewport.width}.png`)});
    }
    await page.setViewportSize({width:390,height:844});
    await page.locator('#momentVisibilityApply').click();
    assert.match(await page.locator('#momentVisibilitySummary').innerText(),/2/);
    await page.locator('#momentVisibilityOpen').click();
    await page.locator('#momentVisibilityList input[value="blocked"]').uncheck();
    await page.keyboard.press('Escape');
    assert.match(await page.locator('#momentVisibilitySummary').innerText(),/2/);
    assert.equal(await page.evaluate(()=>document.activeElement.id),'momentVisibilityOpen');
    failPicker=true;
    await page.locator('#momentVisibilityOpen').click();
    await page.waitForFunction(()=>document.getElementById('momentVisibilityStatus').textContent.includes('失败'));
    assert(await page.locator('#momentVisibilityApply').isDisabled());
    await page.locator('#momentVisibilityCancel').click();failPicker=false;
    failPublish=true;
    await page.locator('#momentComposeBtn').click();
    await page.waitForFunction(()=>!document.getElementById('momentComposeBtn').disabled);
    assert.match(await page.locator('#momentVisibilitySummary').innerText(),/2/);
    assert.equal(await page.locator('#momentComposeText').inputValue(),'这条只给部分角色看');
    failPublish=false;
    await page.locator('#momentComposeBtn').click();
    await page.waitForFunction(()=>getComputedStyle(document.getElementById('momentModalMask')).display==='none');
    assert(requests.at(-1).includes('["blocked","allowed"]'));
    await page.locator('.moment-visibility-badge').first().click();
    assert.equal(await page.locator('#momentVisibilityList strong').count(),2);
    await page.locator('#momentVisibilityCancel').click();
    await page.locator('#openMomentModal').click();
    assert.equal(await page.locator('#momentVisibilitySummary').innerText(),'所有角色可见');
    // Clipboard fallback uses the published post, regardless of the new draft's selection.
    await page.keyboard.press('Escape');
    await page.evaluate(() => {
      document.getElementById('momentModalMask').style.display = 'none';
      window.failClipboard = true;
    });
    assert((await copyFirstPost()).includes('【谁不可见】角色甲、角色乙\n'));
    // Older public posts have no visibility fields and still copy normally.
    posts.splice(0, posts.length, makePost('allowed', []));
    delete posts[0].blocked_character_ids;
    delete posts[0].blocked_characters;
    await page.reload();
    await page.locator('.moment-card').waitFor();
    assert(!(await copyFirstPost()).includes('【谁不可见】'));
    assert.deepEqual(errors,[]);
    console.log('PASS: real Moments page picker/viewer, copy with blocked names and clipboard fallback, legacy public copy, post payload, draft retention, reset, disabled replies and mobile/desktop layout');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
