const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const base='http://127.0.0.1:5098';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{
  const ctx=await browser.newContext({baseURL:base,viewport:{width:390,height:844},serviceWorkers:'block'});
  await ctx.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  const page=await ctx.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  for(const lang of ['ja','en','zh-CN']){
   await ctx.request.post('/api/ui/language',{data:{user_language:lang}});
   for(const route of ['/terms','/privacy']){
    assert.equal((await page.goto(route)).status(),200);
    assert.equal(await page.locator('html').getAttribute('lang'),lang);
    assert(await page.locator('#legal-review-notice').isVisible());
    const text=await page.locator('article').innerText();
    assert(text.includes('2.0-review.1')&&text.includes('2026-09-23'));
    assert(!text.includes('2026年8月11日'));
    if(lang==='en')assert(!/[\u4e00-\u9fff]/.test(text),text);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.locator('footer').scrollIntoViewIfNeeded();assert(await page.locator('footer').isVisible());
   }
   assert.equal((await ctx.request.get('/register')).status(),404);
  }
  assert.equal((await ctx.request.post('/api/login',{data:{username:'test@saki.invalid',password:'synthetic-browser-password'}})).status(),200);
  await page.goto('/profile');
  await page.locator('#legal-section .section-header').click();
  await page.locator('#legal-consent-button').waitFor({state:'visible'});
  assert((await page.locator('#legal-section').innerText()).includes('审阅稿'));
  await page.locator('#legal-consent-button').click();
  await page.locator('#legal-consent-button').waitFor({state:'hidden'});
  const status=await (await ctx.request.get('/api/legal/status')).json();
  assert(status.current);assert.equal(status.documents.terms.version,'2.0-review.1');
  assert.equal(status.documents.terms.effective_date,null);
  assert.equal(status.documents.terms.status,'review');
  assert.deepEqual(errors,[]);
  console.log('PASS: actual terms/privacy in 3 languages, mobile layout, explicit draft status, closed public registration, profile acknowledgement of new version');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
