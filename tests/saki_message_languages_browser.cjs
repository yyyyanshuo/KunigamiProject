const assert=require('node:assert/strict');
const {chromium}=require(process.env.SAKI_PLAYWRIGHT_MODULE || '../agent-test/node_modules/playwright');
const base='http://127.0.0.1:5098';
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,serviceWorkers:'block'});
 await context.route('**/*',r=>r.request().url().startsWith(base+'/')?r.continue():r.abort());
 assert.equal((await context.request.post(base+'/api/login',{data:{username:'test@saki.invalid',password:'synthetic-mobile-review-password'}})).status(),200);
 await context.request.post(base+'/api/characters/add',{data:{id:'mobile_second',name:'Second'}});
 await context.request.post(base+'/api/groups/add',{data:{id:'mobile_group',name:'Mobile group',members:['saki_demo','mobile_second']}});
 const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(const lang of ['ja','en','zh-CN']){
 await context.request.post(base+'/api/ui/language',{data:{user_language:lang}});
 await page.goto(base+'/chat/saki_demo');await page.waitForTimeout(400);
 for(const [language,expected] of [[lang,false],[lang==='en'?'ja':'en',true]]){
  const result=await page.evaluate(({language})=>{
   aiLanguage=language;currentConfig={language};
   document.querySelectorAll('.message-group').forEach(el=>el.remove());
   renderMessageGroup({id:90001,role:'assistant',timestamp:'2026-09-23 23:59:00',content:'Hello there'});
   return {policy:needsReadingTranslation(),buttons:Array.from(document.querySelectorAll('.action-btn')).filter(el=>el.title===SakiI18n.t('翻译')).length};
  },{language});
  assert.equal(result.policy,expected);assert.equal(result.buttons>0,expected,JSON.stringify(result));
 }
 const special=await page.evaluate(()=>{
  const out=[];for(const [id,content] of [[90002,'[TRANSFER:100円|note]'],[90003,'[转账:100円|旧记录]'],[90004,'[STICKER]official:test.png'],[90005,'[IMAGE](test.png)(caption)']]){
   const el=renderMessageGroup({id,role:'assistant',timestamp:'2026-09-23 23:59:00',content});out.push({id,card:!!el.querySelector('.transfer-card'),image:!!el.querySelector('img'),raw:el.dataset.originalContent});
  }
  return out;
 });assert.ok(special[0].card&&special[1].card);assert.equal(special[1].raw,'[转账:100円|旧记录]');assert.ok(special[2].image&&special[3].image);
 await page.goto(base+'/chat/group/mobile_group');await page.waitForTimeout(400);
 for(const [groupLang,memberLang,defaultLang,expected] of [[lang,'en','en',false],['',lang,lang,true],['','',lang,false],['','',lang==='en'?'ja':'en',true]]){
  const got=await page.evaluate(({groupLang,memberLang,defaultLang})=>{currentConfig={language:groupLang};groupMembers={saki_demo:{language:memberLang}};aiLanguage=groupLang||defaultLang;return needsReadingTranslation();},{groupLang,memberLang,defaultLang});assert.equal(got,expected);
 }
 await page.route('**/api/moments?*',r=>r.fulfill({json:[{char_id:'user',remark:'User',avatar:'/user_avatar',timestamp:'2026-09-23 23:30:00',content:'Test post',liker_ids:[],comments:[{commenter_id:'saki_demo',remark:'Saki',content:'Comment',timestamp:'2026-09-23 23:31:00',comment_index:0}]}]}));
 await page.goto(base+'/moments');await page.locator('.moment-card').waitFor();
 const menu=await page.locator('.moment-menu-item').allTextContents();
 if(lang!=='zh-CN')assert.ok(!menu.some(x=>/翻译|重新生成|重新记忆|回复|删除|编辑|复制|点赞|评论/.test(x)),JSON.stringify({lang,menu}));
 assert.ok(await page.locator('.moment-translate-btn').count());assert.ok(await page.locator('.comment-translate-btn').count());
 await page.goto(base+'/');
 if(lang==='ja'){await page.waitForFunction(()=>document.getElementById('globalBedtimeDiaryBtn')?.textContent.trim()==='就寝前まとめ');assert.equal((await page.locator('#globalBedtimeDiaryBtn').textContent()).trim(),'就寝前まとめ');}
 console.log('PASS translation matrix, canonical/legacy cards, moments menus, bedtime label',lang);
 }
 assert.deepEqual(errors,[]);console.log('PASS browser multilingual regression');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
