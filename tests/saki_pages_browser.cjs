const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--disable-background-networking','--disable-sync','--no-first-run']});
 try {
  const context=await browser.newContext({locale:'en-US',serviceWorkers:'block'});
  await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
  const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:5097/login');
  assert.equal(await page.locator('html').getAttribute('lang'),'en');
  const login=await context.request.post('http://127.0.0.1:5097/api/login',{data:{username:'test@saki.invalid',password:'synthetic-browser-password'}});assert.equal(login.status(),200);
  for(const lang of ['en','ja','zh-CN']){
   await context.request.post('http://127.0.0.1:5097/api/ui/language',{data:{user_language:lang}});
   for(const path of ['/profile','/memory/saki_demo','/forum','/square/upload','/moments']){
    await page.goto('http://127.0.0.1:5097'+path,{waitUntil:'domcontentloaded'});await page.waitForTimeout(350);
    assert.equal(await page.locator('html').getAttribute('lang'),lang,path);
    if(path==='/forum') assert.equal(await page.locator('#forumLanguage').inputValue(),lang);
    if(path==='/square/upload') assert.equal(await page.locator('[name="aliases"]').count(),1);
    if(path==='/profile') assert.equal(await page.locator('#furigana-enabled').count(),1);
   }
  }
  await context.request.post('http://127.0.0.1:5097/api/ui/language',{data:{user_language:'en'}});
  for(const path of ['/profile','/memory/saki_demo','/forum','/square/upload','/moments','/square','/chat/saki_demo','/map']) {
    await page.goto('http://127.0.0.1:5097'+path,{waitUntil:'domcontentloaded'}); await page.waitForTimeout(400);
    const lines=(await page.locator('body').innerText()).split('\n').map(s=>s.trim()).filter(s=>/[\u4e00-\u9fff]/.test(s));
    const allowed = lines.filter(line => !line.includes('小咲（本地测试）') && line !== '🔌 线路与模型设置 (API)');
    assert.deepEqual(allowed,[],path);
    if(path==='/square') { const response=await context.request.get('http://127.0.0.1:5097/api/square/list'); assert.equal(response.status(),200); assert(Array.isArray(await response.json())); }
  }
  await context.request.post('http://127.0.0.1:5097/api/user/profile_settings',{data:{default_chat_language:'ja'}});
  await page.goto('http://127.0.0.1:5097/chat/saki_demo',{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>!document.getElementById('transBtn').disabled);
  let sends=0;page.on('request',request=>{if(request.method()==='POST' && /\/messages$/.test(new URL(request.url()).pathname)) sends++;});
  await page.route('**/api/translate',async route=>{await new Promise(resolve=>setTimeout(resolve,250));await route.fulfill({json:{result:'Translated test draft'}});});
  await page.locator('#transInput').fill('Original test text');await page.locator('#messageInput').fill('Existing draft');
  let confirmed=0;page.once('dialog',async dialog=>{confirmed++;await dialog.dismiss();});
  await page.locator('#transBtn').click();await page.locator('#messageInput').fill('');
  await page.waitForFunction(()=>!document.getElementById('transBtn').disabled);
  assert.equal(confirmed,1);assert.equal(await page.locator('#messageInput').inputValue(),'');assert.equal(await page.locator('#transInput').inputValue(),'Original test text');
  await page.locator('#messageInput').fill('Another draft');page.once('dialog',dialog=>dialog.accept());
  await page.locator('#transBtn').click();await page.waitForFunction(()=>!document.getElementById('transBtn').disabled);
  assert.equal(await page.locator('#messageInput').inputValue(),'Translated test draft');assert.equal(sends,0);
  assert.deepEqual(errors,[]);
  console.log('PASS live local Flask + Chrome: login and 23 rendered pages, UI locale, forum default, alias form, furigana control, translation draft confirmation and no automatic send; no page errors');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
