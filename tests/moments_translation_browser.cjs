/* Actual Moments UI with offline fixtures; no live accounts or model calls. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const template = fs.readFileSync(path.join(root, 'templates/moments.html'), 'utf8');
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'static/locales/ui.json'), 'utf8'));
function html(owner, language) {
  const translations = Object.fromEntries(Object.entries(catalog).map(([key, values])=>[key, values[language] || key]));
  return template.replace(/\{%[\s\S]*?%\}/g, '')
    .replace(/\{\{\s*user_timezone[^}]*\}\}/g, '"Asia/Shanghai"')
    .replace(/\{\{\s*ui_language\s*\}\}/g, language)
    .replace(/\{\{[\s\S]*?\}\}/g, '')
    .replace(/<script[^>]+src="[^"]*(?:auth-guard|ai-disclosure|ai-cruise|theme-loader|voice-call-incoming)[^"]*"[^>]*><\/script>/g, '')
    .replace('<head>', () => `<head><style>#theme-loading-screen {display:none!important;} .moment-card {animation:none!important;}</style>
      <script>window.SakiPrivacyUser=${JSON.stringify(owner)};window.SakiUICatalog=${JSON.stringify(translations).replace(/</g,'\\u003c')};</script>
      <script src="/static/ui-language.js"></script><script src="/static/privacy-session.js"></script>`);
}
function post() {
  return {char_id:'author',timestamp:'2026-10-19 10:00:00',remark:'作者',content:'今日もいい天気。',
    avatar:'/static/default_avatar.png',liker_ids:[],liker_remarks:[],blocked_character_ids:[],
    comments:[{comment_index:2,commenter_id:'reader',remark:'评论者',content:'散歩しよう。',timestamp:'2026-10-19 10:01:00'}]};
}
(async()=>{
  const browser = await chromium.launch({headless:true, executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  let failures = 0;
  async function check(name, run, options = {}) {
    const context = await browser.newContext({viewport:{width:390,height:844}, serviceWorkers:'block'});
    const page = await context.newPage();
    page.setDefaultTimeout(4000);
    const state = {posts:[post()],requests:[],errors:[],confirms:[],acceptConfirm:false,
      respond:async route=>route.fulfill({json:{result:'译文 '+state.requests.length}})};
    page.on('pageerror',error=>state.errors.push(error.message));
    page.on('dialog',async dialog=>{
      state.confirms.push(dialog.message());
      if(state.acceptConfirm) await dialog.accept(); else await dialog.dismiss();
    });
    if(options.noStorage) await page.addInitScript(()=>{
      Object.defineProperty(window, 'localStorage', {get(){throw new DOMException('Unavailable', 'SecurityError');}});
    });
    await context.route('**/*', async route=>{
      const url = new URL(route.request().url());
      const json = value=>route.fulfill({json:value});
      if(url.pathname==='/moments') return route.fulfill({contentType:'text/html; charset=utf-8',body:html(url.searchParams.get('owner')||'1',url.searchParams.get('lang')||'zh-CN')});
      if(url.pathname.startsWith('/static/')) {
        const file = path.join(root,url.pathname);
        return fs.existsSync(file) ? route.fulfill({path:file}) : route.fulfill({status:404,body:''});
      }
      if(url.pathname==='/api/vision/prompts') return json({prompts:[]});
      if(url.pathname==='/api/user/profile_settings') return json({ai_language:'ja'});
      if(url.pathname==='/api/moments/active_enabled') return json({enabled:false});
      if(url.pathname==='/api/moments/characters'||url.pathname==='/api/moments/related_characters') return json({author:'作者',reader:'评论者'});
      if(url.pathname==='/api/moments') return json(state.posts);
      if(url.pathname==='/api/translate') {state.requests.push(route.request().postDataJSON()); return state.respond(route);}
      if(url.pathname==='/api/moments/edit') {state.posts[0].content=route.request().postDataJSON().new_content; return json({status:'success'});}
      if(url.pathname==='/api/moments/comment/edit') {state.posts[0].comments[0].content=route.request().postDataJSON().new_content; return json({status:'success'});}
      if(url.pathname==='/api/moments/like') return json({status:'success'});
      throw new Error('Unexpected fixture request: '+url.pathname);
    });
    const load = async(owner='1', language='zh-CN')=>{
      await page.goto(`http://moments.test/moments?owner=${owner}&lang=${language}`);
      await page.locator('.moment-card').first().waitFor();
    };
    const menu = async(comment=false, action='translate')=>{
      const scope = page.locator(comment ? '.comment-item .moment-actions-menu' : '.moment-head .moment-actions-menu').first();
      await scope.locator('.moment-menu-btn').click();
      await scope.locator(`.${comment?'comment':'moment'}-${action}-btn`).click();
    };
    const result = comment=>page.locator(comment ? '.comment-translation-result' : '.moment-translation-result').first();
    const translated = async(comment=false)=>{await menu(comment); await result(comment).waitFor();};
    const edit = async(comment=false, content='変更後の文章。')=>{
      await menu(comment,'edit');
      await page.locator('#editModalTextarea').fill(content);
      await page.locator('#editModalSubmit').click();
      await page.waitForFunction(()=>getComputedStyle(document.getElementById('editModalMask')).display==='none');
      await page.locator(comment ? '.comment-text' : '.moment-content').first().filter({hasText:content}).waitFor();
    };
    try {
      await load();
      await run({page,state,load,menu,result,translated,edit});
      assert.deepEqual(state.errors,[]);
      console.log('PASS: '+name);
    } catch(error) {failures++; console.error('FAIL: '+name+'\n'+error.stack+'\nPage errors: '+JSON.stringify(state.errors));}
    finally {await context.close();}
  }
  try {
    await check('post and sparse comment index: read, hide, fresh translation, reload',async({page,state,load,menu,result,translated})=>{
      for(const comment of [false,true]) {
        await translated(comment);
        const request=state.requests.at(-1);
        assert.equal(request.purpose,'read');assert.equal(request.scene,'moments');
        assert.equal(request.text,comment?'散歩しよう。':'今日もいい天気。');
        const count=state.requests.length;
        await menu(comment);assert.equal(await result(comment).count(),0);
        assert.equal(state.requests.length,count);
        await load();assert.equal(await result(comment).count(),0);
        await translated(comment);assert.equal(state.requests.length,count+1);
        await load();assert.equal(await result(comment).innerText(),'译文 '+state.requests.length);
      }
    });
    await check('editing post/comment invalidates translations, including after reload',async({load,result,translated,edit})=>{
      for(const comment of [false,true]) {
        await translated(comment);await edit(comment);
        assert.equal(await result(comment).count(),0,'Edited source must not show the old translation');
        await load();assert.equal(await result(comment).count(),0);
      }
    });
    await check('HTTP and invalid model results show errors and allow retry',async({page,state,menu,result,translated})=>{
      for(const comment of [false,true]) {
        state.respond=route=>route.fulfill({status:504,contentType:'text/html',body:'<html>Gateway timeout</html>'});
        await menu(comment);
        await page.locator('.moment-toast').filter({hasText:'504'}).last().waitFor();
        assert.equal(await result(comment).count(),0);
        state.respond=route=>route.fulfill({json:{result:'  '}});
        await menu(comment);
        await page.locator('.moment-toast').filter({hasText:'HTTP 200'}).last().waitFor();
        assert.equal(await result(comment).count(),0);
        state.respond=route=>route.fulfill({json:{result:'重试成功'}});
        await translated(comment);assert.equal(await result(comment).innerText(),'重试成功');
      }
    });
    await check('account and language caches remain isolated and restore on return',async({state,load,result,translated})=>{
      await translated();const first=await result(false).innerText();
      await load('2');assert.equal(await result(false).count(),0);
      await translated();const second=await result(false).innerText();
      await load('1');assert.equal(await result(false).innerText(),first);
      await load('1','en');assert.equal(await result(false).count(),0);
      await translated();
      await load('1');assert.equal(await result(false).innerText(),first);
      await load('2');assert.equal(await result(false).innerText(),second);
      assert.equal(state.requests.length,3);
    });
    await check('unavailable browser storage does not prevent feed or translation',async({translated})=>{
      await translated();
    },{noStorage:true});
    await check('image metadata stays out of translations, image-only posts need no request',async({page,state,load,menu,translated})=>{
      const image='[IMAGE](/static/logo.png)(不应外露的图片描述)';
      state.posts[0].content='今日の写真。'+image;
      await load();await translated();
      assert.equal(state.requests.at(-1).text,'今日の写真。');
      state.posts[0].content=image;
      await load();await menu();
      await page.locator('.moment-toast').filter({hasText:'没有可翻译的文字'}).waitFor();
      assert.equal(state.requests.length,1);
    });
    await check('late reading results cannot overwrite edited posts/comments',async({page,state,menu,result,edit})=>{
      for(const comment of [false,true]) {
        let release;
        state.respond=route=>new Promise(resolve=>{release=async()=>{await route.fulfill({json:{result:'过期译文'}});resolve();};});
        const requested=page.waitForRequest('**/api/translate');
        await menu(comment);await requested;
        assert(release);
        await edit(comment);
        const response=page.waitForResponse('**/api/translate');
        await release();await response;
        await page.waitForFunction(()=>!document.querySelector('.moment-translate-btn:disabled, .comment-translate-btn:disabled'));
        assert.equal(await result(comment).count(),0);
      }
    });
    await check('feed rerenders keep pending buttons disabled and prevent duplicate requests',async({page,state,menu,translated})=>{
      let release;
      state.respond=route=>new Promise(resolve=>{release=async()=>{await route.fulfill({json:{result:'等待后的译文'}});resolve();};});
      const requested=page.waitForRequest('**/api/translate');
      await menu();await requested;
      await menu(false,'like');
      await page.locator('.moment-translate-btn[disabled]').waitFor({state:'attached'});
      assert.equal(state.requests.length,1);
      await release();await page.locator('.moment-translation-result').waitFor();
      assert.equal(await page.locator('.moment-translation-result').innerText(),'等待后的译文');
    });
    await check('comment index reuse does not display another comment’s cached translation',async({state,load,result,translated})=>{
      await translated(true);
      state.posts[0].comments[0]={...state.posts[0].comments[0],content:'別のコメント。',timestamp:'2026-10-19 10:02:00'};
      await load();assert.equal(await result(true).count(),0);
      await translated(true);assert.equal(state.requests.at(-1).text,'別のコメント。');
    });
    await check('compose translation preserves drafts, updates count, and never sends automatically',async({page,state})=>{
      await page.locator('#openMomentModal').click();
      await page.locator('#momentComposeTransText').fill('准备翻译的草稿');
      await page.locator('#momentComposeText').fill('手写草稿');
      await page.locator('#momentComposeTransBtn').click();
      await page.waitForFunction(()=>!document.getElementById('momentComposeTransBtn').disabled);
      assert.equal(await page.locator('#momentComposeText').inputValue(),'手写草稿');
      assert.equal(await page.locator('#momentComposeTransText').inputValue(),'准备翻译的草稿');
      assert.equal(state.confirms.length,1);
      state.acceptConfirm=true;
      await page.locator('#momentComposeTransBtn').click();
      await page.waitForFunction(()=>!document.getElementById('momentComposeTransBtn').disabled);
      assert.equal(state.requests.at(-1).purpose,'send');
      assert.equal(await page.locator('#momentComposeText').inputValue(),'译文 2');
      assert.match(await page.locator('#momentCharCount').innerText(),/^4\//);
      state.respond=route=>route.fulfill({status:500,json:{error:'测试翻译错误'}});
      await page.locator('#momentComposeTransText').fill('失败时留下原文');
      await page.locator('#momentComposeTransBtn').click();
      await page.locator('.moment-toast').filter({hasText:'测试翻译错误'}).waitFor();
      assert.equal(await page.locator('#momentComposeTransText').inputValue(),'失败时留下原文');
      assert.equal(await page.locator('#momentComposeText').inputValue(),'译文 2');
      // Any unexpected POST, including publishing, fails the fixture router.
    });
    await check('reply translation includes target comment and stays in its original dialog',async({page,state,menu})=>{
      await page.locator('.comment-item .moment-menu-btn').click();
      await page.locator('.comment-reply-ai-btn').click();
      await page.locator('#showUserReplyBtn').click();
      await page.locator('#userReplyTransInput').fill('我也一起去');
      await page.locator('#userReplyTransBtn').click();
      await page.waitForFunction(()=>!document.getElementById('userReplyTransBtn').disabled);
      assert.equal(state.requests.at(-1).purpose,'send');
      assert(state.requests.at(-1).context.includes('散歩しよう。'));
      assert.equal(await page.locator('#userReplyTextarea').inputValue(),'译文 1');
      assert.equal(await page.locator('#userReplyCharCount').innerText(),'4/500');
      let release;
      state.respond=route=>new Promise(resolve=>{release=async()=>{await route.fulfill({json:{result:'不该出现在新弹窗'}});resolve();};});
      await page.locator('#userReplyTransInput').fill('等待返回');
      const requested=page.waitForRequest('**/api/translate');
      await page.locator('#userReplyTransBtn').click();await requested;
      await page.locator('#userReplyCancel').click();
      await page.locator('.moment-head .moment-menu-btn').click();
      await page.locator('.post-reply-ai-btn').click();
      await page.locator('#showUserReplyBtn').click();
      const response=page.waitForResponse('**/api/translate');
      await release();await response;
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      assert.equal(await page.locator('#userReplyTextarea').inputValue(),'');
      assert.equal(await page.locator('#userReplyTransBtn').isEnabled(),true);
      assert.equal(state.confirms.length,0);
    });
  } finally {await browser.close();}
  if(failures) throw new Error(`${failures} Moments translation checks failed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
