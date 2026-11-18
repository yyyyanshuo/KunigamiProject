const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const base='http://127.0.0.1:5098';
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--disable-background-networking','--disable-sync','--no-first-run']});
 try {
  const context=await browser.newContext({baseURL:base,locale:'en-US',serviceWorkers:'block',viewport:{width:390,height:844}});
  await context.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  async function go(path){await page.goto(path,{waitUntil:'domcontentloaded'});await page.waitForTimeout(250);}
  async function corner(){const box=await page.locator('.saki-language-setting select').boundingBox();assert(box.x<25&&box.y<25&&box.width<=110&&box.height<=36,JSON.stringify(box));assert.equal(await page.locator('.saki-language-setting select').evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(255, 255, 255)');}
  await go('/login');await corner();await go('/forgot_password');await corner();
  await page.locator('.saki-language-setting select').selectOption('ja');await page.waitForFunction(()=>document.documentElement.lang==='ja');
  assert.equal((await context.request.post('/api/login',{data:{username:'test@saki.invalid',password:'synthetic-browser-password'}})).status(),200);
  await go('/profile');await corner();assert.equal(await page.locator('.saki-language-setting select').inputValue(),'ja');
  assert.equal(await page.locator('#val-wechat,img[src*="qq_qr"],img[src*="xhs_qr"]').count(),0);assert.equal(await page.locator('#val-email').innerText(),'yyyyanshuo@foxmail.com');
  await context.request.post('/api/characters/add',{data:{id:'saki_other',name:'Synthetic other'}});
  await context.request.post('/api/groups/add',{data:{id:'feedback_group',name:'Synthetic group',members:['saki_demo','saki_other']}});
  async function prefs(ui,def,role='',group=''){
   for(const [path,data] of [['/api/ui/language',{user_language:ui}],['/api/user/profile_settings',{default_chat_language:def}],['/api/saki_demo/update_meta',{language:role}],['/api/group/feedback_group/update_meta',{language:group}]]){
    const r=await context.request.post(path,{data});assert.equal(r.status(),200,await r.text());
   }
  }
  for(const [ui,def,role,group,single,groupExpected] of [
    ['ja','','','',false,false],['en','ja','','',true,true],['en','ja','English','en',false,false],['zh-CN','zh','','',false,false],['ja','ja','Español','',true,false],['ja','ja','','en',false,true]
  ]){
   await prefs(ui,def,role,group);
   for(const [path,visible] of [['/chat/saki_demo',single],['/chat/group/feedback_group',groupExpected]]){
    await go(path);await page.waitForTimeout(350);
    assert.equal(await page.locator('#translation-input-container').isVisible(),visible,JSON.stringify([path,ui,def,role,group]));
   }
  }
  await context.route('**/api/moments?*',r=>r.fulfill({json:[{char_id:'saki_demo',remark:'Synthetic demo',avatar:'/static/default_avatar.png',timestamp:'2026-09-22 08:00:00',content:'Synthetic post',comments:[{commenter_id:'saki_other',remark:'Synthetic other',comment_index:1,content:'Synthetic comment'}],liker_ids:[]}]}));
  for(const [ui,def,expected] of [['ja','',false],['en','ja',true],['zh-CN','en',true],['zh-CN','Chinese',false]]){
    await prefs(ui,def,'Français','');await go('/moments');await page.locator('.moment-card').first().waitFor();
    await page.locator('#openMomentModal').click();assert.equal(await page.locator('#momentComposeTransRow').isVisible(),expected);
    await page.locator('#momentComposeText').fill('Cancel this draft');await page.locator('#momentModalCancel').click();assert.equal(await page.locator('#momentModalMask').isVisible(),false);assert.equal(await page.locator('#momentComposeText').inputValue(),'');
    await page.evaluate(()=>window.openReplyModal('saki_demo','2026-09-22 08:00:00',1));await page.locator('#showUserReplyBtn').click();
    assert.equal(await page.locator('#userReplyTransRow').isVisible(),expected);await page.locator('#userReplyTextarea').fill('Cancel comment');await page.locator('#userReplyCancel').click();assert.equal(await page.locator('#replyModalMask').isVisible(),false);
  }
  for(const lang of ['en','ja','zh-CN']){
   await prefs(lang,'ja','en','');await go('/memory/saki_demo');
   await page.locator('#module-relation summary').click();await page.locator('#module-relation .btn-edit').click();await page.locator('[data-relation-editor-mode="cards"]').waitFor();
   const labels=await page.evaluate(()=>['我的视角','他人视角（他们如何看我）','卡片编辑','AI 补全关系'].map(k=>SakiI18n.t(k)));
   const editor=page.locator('#module-relation .edit-mode');for(const label of labels)assert((await editor.innerText()).includes(label),lang+': '+label);
   if(lang==='en')assert(!/[\u4e00-\u9fff]/.test(await editor.innerText()),await editor.innerText());
   await page.locator('[data-relation-editor-mode="json"]').click();assert((await editor.innerText()).includes('JSON'));
   if(lang==='en')assert(!/[\u4e00-\u9fff]/.test(await editor.innerText()),await editor.innerText());
   await page.evaluate(()=>{window.__copied='';window.writeTextToClipboard=async text=>{window.__copied=text;};});
   await page.locator('#module-relation [onclick="copyRelationshipPrompt(this)"]').click();await page.waitForFunction(()=>window.__copied.length>0);
   const prompt=await page.evaluate(()=>window.__copied);assert(prompt.includes('Synthetic other'));assert(prompt.includes('en'));if(lang==='en')assert(prompt.startsWith('Using the character settings'));
   await page.locator('[data-relation-mode="reverse"]').click();await page.evaluate(()=>window.__copied='');await page.locator('#module-relation [onclick="copyRelationshipPrompt(this)"]').click();await page.waitForFunction(()=>window.__copied.length>0);assert((await page.evaluate(()=>window.__copied)).includes('output_language'));
  }
  await context.request.post('/api/ui/language',{data:{user_language:'zh-CN'}});await go('/profile');
  const staticText=await page.locator('body').innerText();assert(!staticText.includes('(API)'));
  assert.deepEqual(errors,[]);console.log('PASS feedback browser: small white language selectors, email-only feedback, 12 single/group language combinations, Moments/comment translation and cancel, 3-locale relationship controls and copied prompts, Chinese labels');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
