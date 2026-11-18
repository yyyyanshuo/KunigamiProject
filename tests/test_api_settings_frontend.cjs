// Integration test against an isolated SAKI_LOCAL_TEST server; no external traffic.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE||'C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const base=process.env.SAKI_BROWSER_URL||'http://127.0.0.1:5098';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{
  const context=await browser.newContext({baseURL:base,viewport:{width:390,height:844},serviceWorkers:'block'});
  await context.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  assert.equal((await context.request.post('/api/login',{data:{username:'test@saki.invalid',password:'synthetic-browser-password'}})).status(),200);
  const page=await context.newPage(),errors=[];page.on('dialog',dialog=>dialog.accept());page.on('pageerror',e=>errors.push(e.message));
  const module=page.locator('#module-api');
  async function open(){await page.goto('/profile');await page.waitForFunction(()=>document.querySelectorAll('#model-settings-overview .api-status-item').length===10);await module.locator('.section-header').click();await module.locator('.btn-edit-toggle').click();}
  async function route(name){await page.locator('#radio-'+name).check({force:true});}
  async function fillModel(task,value){await page.locator('#model-'+task).selectOption('__custom__');await page.locator('#custom-model-'+task).fill(value);}
  async function save(){await module.locator('.btn-save').click();await page.locator('#api-edit-mode').waitFor({state:'hidden'});}
  await open();
  assert.equal(await page.locator('#gemini-key-input,#openrouter-key-input').count(),0);
  assert.equal(await module.locator('input[type=password]').count(),1);
  assert.equal(await page.locator('#model-chat').inputValue(),'gemini-3.8-flash');
  assert.equal(await page.locator('#model-call').inputValue(),'gemini-3.8-flash');
  assert.equal(await page.locator('#image-daily-limit').inputValue(),'10');
  assert.equal(await page.locator('#model-summary').inputValue(),'gemini-3-flash-preview');
  assert.equal(await page.locator('#model-image').inputValue(),'gemini-nano-banana-2.1');
  for(const [model,tier] of [['gemini-4-hydrogen','Flash'],['gemini-4-carbon','Pro'],['gemini-4-argon','Pro']]){
   const label=await page.locator('#model-chat option[value="'+model+'"]').evaluate(e=>e.parentElement.label);
   assert(label.startsWith(tier),label);
  }
  for(const lang of ['zh-CN','en','ja']){
   await context.request.post('/api/ui/language',{data:{user_language:lang}});await open();
   for(const name of ['gemini','openai','anthropic','xai','custom']){
    await route(name);assert.equal(await module.locator('.model-task-card').count(),9);
    assert.equal(await module.locator('input[type=password]').count(),1);
    if(name==='anthropic'){assert(await page.locator('#model-image').isDisabled());assert.equal(await page.locator('#model-image').inputValue(),'');}
    assert.equal(await page.locator('#custom-route-settings').isVisible(),name==='custom');
    assert(await module.evaluate(e=>e.querySelector('.action-bar').getBoundingClientRect().bottom<=e.getBoundingClientRect().bottom));
    await module.locator('.btn-save').click({trial:true});
    await module.locator('.btn-cancel').click({trial:true});
    if(['openai','anthropic','xai'].includes(name)){
     const expected={openai:7,anthropic:9,xai:4}[name];
     assert.equal(await page.locator('#model-chat optgroup option').count(),expected);
     assert(await page.locator('#model-chat optgroup').count()>=3);
     assert(!(await page.locator('#model-chat option').allTextContents()).some(v=>v.includes('image')));
     const selected=await page.locator('#model-chat').inputValue();
     assert.equal(await module.locator('.model-description,.model-task-hint').count(),0);
     await page.locator('#model-chat').selectOption({openai:'gpt-6-astra',anthropic:'claude-fable-5-1',xai:'grok-4.3'}[name]);
     await page.locator('#model-chat').locator('..').locator('.model-reset').click();
     assert.equal(await page.locator('#model-chat').inputValue(),selected);
     if(lang==='en')assert(!/[\u4e00-\u9fff]/.test(await module.innerText()),await module.innerText());
    }

   }
   if(lang==='en')assert(!/[\u4e00-\u9fff]/.test(await module.innerText()),await module.innerText());
   assert(await module.evaluate(e=>e.scrollWidth<=e.clientWidth+2));
  }
  // Tallest editor: narrow viewport and every custom input open.
  await page.setViewportSize({width:320,height:640});await route('custom');
  for(const task of ['chat','translation','vision','gen_persona','call','moments','summary','image','forum'])await page.locator('#model-'+task).selectOption('__custom__');
  assert(await module.evaluate(e=>e.querySelector('.action-bar').getBoundingClientRect().bottom<=e.getBoundingClientRect().bottom));
  await module.locator('.btn-save').click({trial:true});await module.locator('.btn-cancel').click({trial:true});
  await module.locator('.section-header').click();assert(!(await module.locator('.section-body').isVisible()));
  await module.locator('.section-header').click();await module.locator('.btn-cancel').click();
  await module.locator('.btn-edit-toggle').click();await page.setViewportSize({width:1280,height:900});
  await module.locator('.btn-save').click({trial:true});
  // Independent route drafts, including unsaved keys and the shared daily image limit.
  await page.locator('#image-daily-limit').fill('20');
  await route('gemini');await fillModel('chat','private-gemini-model');await page.locator('#route-key-input').fill('synthetic-gemini-1111');
  await route('openai');assert.equal(await page.locator('#image-daily-limit').inputValue(),'20');await fillModel('chat','private-openai-model');await page.locator('#route-key-input').fill('synthetic-openai-2222');
  await route('gemini');assert.equal(await page.locator('#custom-model-chat').inputValue(),'private-gemini-model');assert.equal(await page.locator('#route-key-input').inputValue(),'synthetic-gemini-1111');
  await route('custom');await page.locator('#custom-route-url').fill('https://relay.example/v1');await page.locator('#custom-route-protocol').selectOption('anthropic');await page.locator('#route-key-input').fill('synthetic-custom-3333');
  await fillModel('chat','custom-model');await page.locator('#model-image').selectOption('');await save();
  let config=await(await context.request.get('/api/system_config')).json();
  assert.equal(config.image_daily_limit,20);
  assert.equal(config.active_route,'custom');assert.equal(config.routes.gemini.models.chat,'private-gemini-model');assert.equal(config.routes.openai.models.chat,'private-openai-model');assert.equal(config.routes.custom.models.image,'');assert.equal(config.routes.custom.protocol,'anthropic');
  assert(!JSON.stringify(config).includes('synthetic-'));
  let creds=(await(await context.request.get('/api/user/profile_settings')).json()).credentials;
  assert.equal(creds.gemini.last_four,'1111');assert.equal(creds.openai.last_four,'2222');assert.equal(creds.openrouter.last_four,'3333');
  await open();assert.equal(await page.locator('#route-key-input').inputValue(),'');
  assert.equal(await page.locator('#image-daily-limit').inputValue(),'20');
  await page.locator('#image-daily-limit').fill('30');
  // Cancel discards model edits, staged key replacement, and deletion.
  await fillModel('chat','cancel-me');await page.locator('#route-key-input').fill('cancelled-secret');await page.locator('#route-key-delete').click();
  await module.locator('.btn-cancel').click();await module.locator('.btn-edit-toggle').click();
  assert.equal(await page.locator('#model-chat').inputValue(),'custom-model');assert.equal(await page.locator('#route-key-input').inputValue(),'');
  assert((await page.locator('#route-key-status').innerText()).includes('3333'));
  assert.equal(await page.locator('#image-daily-limit').inputValue(),'20');
  await page.locator('#image-daily-limit').fill('0');await module.locator('.btn-save').click();
  assert(await page.locator('#api-edit-mode').isVisible());
  assert.equal((await(await context.request.get('/api/system_config')).json()).image_daily_limit,20);
  await page.locator('#image-daily-limit').fill('20');
  // Rejected URL retains edits and does not update credentials.
  await page.locator('#custom-route-url').fill('http://localhost/v1');await page.locator('#route-key-input').fill('must-not-save-4444');
  await module.locator('.btn-save').click();await page.waitForFunction(()=>document.querySelector('#module-api .btn-save').disabled===false);
  assert(await page.locator('#api-edit-mode').isVisible());
  creds=(await(await context.request.get('/api/user/profile_settings')).json()).credentials;assert.equal(creds.openrouter.last_four,'3333');
  await page.locator('#custom-route-url').fill('https://relay.example/v1');await page.locator('#route-key-input').fill('');await page.locator('#route-key-delete').click();await save();
  creds=(await(await context.request.get('/api/user/profile_settings')).json()).credentials;assert.equal(creds.openrouter.configured,false);assert.equal(creds.openai.configured,true);
  assert.deepEqual(errors,[]);
  console.log('PASS: 3 locales × 5 routes; one key input; nine model controls; expanded catalogue groups; compact cards; unclipped editor and reachable buttons; restore recommendation; draft isolation; encrypted save; shared quota save and validation; cancel; failed-save retention; key deletion; mobile width');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
