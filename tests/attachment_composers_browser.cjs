/* Production composer DOM and functions with synthetic requests; no live services. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE||'playwright');
const root=path.join(__dirname,'..'),source=fs.readFileSync(path.join(root,'templates/chat.html'),'utf8');
function section(a,b){const start=source.indexOf(a),end=source.indexOf(b,start);assert(start>=0&&end>start,a);return source.slice(start,end);}
const css=Array.from(source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g),m=>m[1]).join('\n') + '\n' + fs.readFileSync(path.join(root, 'static/composer-surfaces.css'), 'utf8');
const more=section('<div id="morePanel"','<!-- 【新增】常用语面板');
const markup=section('<div id="transferActionModal"','<!-- 【新增】底部多选操作栏 -->').replace(/\{\{[\s\S]*?\}\}/g,'');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE||undefined});
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[],sends=[];let prompts=[];
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  await page.route('**/*',route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/api/vision/prompts'){
    if(route.request().method()==='POST'){
     const {action,prompt}=route.request().postDataJSON();
     if(action==='add'&&!prompts.includes(prompt))prompts.push(prompt);
     if(action==='delete')prompts=prompts.filter(p=>p!==prompt);
    }
    return route.fulfill({json:{prompts}});
   }
   return route.fulfill({contentType:'text/html',body:`<!doctype html><meta charset="utf-8"><style>${css}</style><textarea id="draft"></textarea><button id="sendBtn">发送</button>${more}${markup}`});
  });
  await page.goto('http://saki.test/');
  await page.evaluate(()=>{window.SakiI18n={t:key=>key};window.failSend=false;window.sent=[];window.incomingRequests=[];window.translationResult='Translated text';});
  for(const file of ['image-tags.js','image-composer.js','vision-prompts.js','composer-viewport.js','transfer-composer.js'])await page.addScriptTag({path:path.join(root,'static',file)});
  await page.addScriptTag({content:`
   const input=document.getElementById('draft'),btn=document.getElementById('sendBtn');
   let activeTransferAction=null; const isGroupMode=false,charId='fixture',currentId='fixture';
   const chatContainer={scrollTop:0,scrollHeight:100};
   const options={getDraft:()=>input.value,getDraftSource:()=>null,translationSettings:()=>({enabled:true,placeholder:'Translate'}),
    translate:async()=>window.translationResult,clearDraft:old=>{if(input.value===old)input.value='';},canSend:()=>!btn.disabled,
    lock:value=>{btn.disabled=value;},send:async(message,persist,original)=>{if(window.failSend)throw new Error('Synthetic save failure');window.sent.push({message,original});persist();}};
   const transferCaption=SakiTransferComposer.captionEditor('transferAction',options);
   const transferComposer=SakiTransferComposer.create(options);
   const imageComposer=SakiImageComposer.create({...options,resizeTranslationInput:()=>{},splitBubbles:text=>[text],fetch:window.fetch});
   const closeAllPanels=()=>document.getElementById('morePanel').classList.remove('open');
   ${section('    window.morePanelAction =', '    let stickerCurrentTab =')}
   window.openTransfer=()=>transferComposer.open();window.openImage=()=>imageComposer.open(new File(['synthetic'],'fixture.png',{type:'image/png'}));
   const replaceRenderedMessageGroup=()=>{},refreshRenderedMessageById=async()=>{},showAffinityToast=()=>{},pollNewMessages=async()=>{};
   const fetchWithTimeout=async(url,options)=>{window.incomingRequests.push(JSON.parse(options.body));return {ok:true,json:async()=>({transfer_update:{source_id:9,content:'resolved'}})};};
   ${section('  function openTransferActionModal(', '  function needsReadingTranslation()')}
   window.openIncoming=()=>{const card=document.createElement('div');card.className='transfer-card pending';document.body.append(card);openTransferActionModal(9,'12.50USD','Lunch',card);};
  `});
  await page.setViewportSize({width:320,height:568});
  await page.evaluate(()=>document.getElementById('morePanel').classList.add('open'));
  await page.waitForFunction(()=>document.getElementById('morePanel').getBoundingClientRect().height>=131);
  assert.equal(await page.locator('#morePanel .more-item').count(),6);
  const panelFits=await page.locator('#morePanel').evaluate(e=>Array.from(e.querySelectorAll('.more-item')).every(item=>{const b=item.getBoundingClientRect(),p=e.getBoundingClientRect();return b.left>=p.left&&b.right<=p.right&&b.bottom<=p.bottom;}));assert(panelFits);
  await page.locator('#draft').fill('My draft');await page.locator('#morePanel [data-action="transfer"]').click();
  assert.equal(await page.locator('#transferSendCaption').inputValue(),'My draft');
  await page.locator('#transferSendAmount').fill('-1');await page.locator('#transferSendConfirm').click();assert.equal(await page.evaluate(()=>sent.length),0);
  await page.locator('#transferSendAmount').fill('12.50');await page.locator('#transferSendCurrency').fill('USD');await page.locator('#transferSendNote').fill('Lunch');
  await page.evaluate(()=>window.failSend=true);await page.locator('#transferSendConfirm').click();assert(await page.locator('#transferSendModal').isVisible());assert.equal(await page.locator('#draft').inputValue(),'My draft');
  await page.evaluate(()=>window.failSend=false);await page.locator('#transferSendConfirm').click();
  assert.equal(await page.evaluate(()=>sent[0].message),'My draft / [TRANSFER:12.50USD|Lunch]');assert.equal(await page.locator('#draft').inputValue(),'');
  await page.locator('#draft').fill('Keep on cancel');await page.evaluate(()=>openTransfer());await page.locator('#transferSendCancel').click();assert.equal(await page.locator('#draft').inputValue(),'Keep on cancel');
  for(const action of ['accept','return']){
   await page.evaluate(()=>openIncoming());await page.locator('#transferActionCaption').fill('Thank you / extra');
   await page.locator(`[onclick="submitTransferAction('${action}')"]`).click();
   const payload=await page.evaluate(()=>incomingRequests.at(-1));assert.equal(payload.transfer_source_id,9);assert.equal(payload.transfer_action,action);assert(payload.message.startsWith('Thank you / extra / ['));
  }
  await page.evaluate(()=>openTransfer());await page.locator('#transferSendTranslationInput').fill('Original');await page.locator('#transferSendTranslationButton').click();assert.equal(await page.locator('#transferSendCaption').inputValue(),'Translated text');
  await page.locator('#transferSendCancel').click();
  // Saved prompt selection survives closing and reopening the image window.
  await page.evaluate(()=>openImage());await page.locator('#imageVisionPrompt').fill('Focus on clothes');await page.getByRole('button',{name:'保存提示词',exact:true}).click();
  await page.getByRole('status').filter({hasText:'提示词已保存'}).waitFor();await page.locator('#imageDescriptionCancel').click();
  await page.evaluate(()=>openImage());await page.locator('#imagePromptPresets select option[value="Focus on clothes"]').waitFor({state:'attached'});
  await page.locator('#imagePromptPresets select').selectOption('Focus on clothes');assert.equal(await page.locator('#imageVisionPrompt').inputValue(),'Focus on clothes');
  await page.getByRole('button',{name:'删除所选',exact:true}).click();await page.getByRole('status').filter({hasText:'提示词已删除'}).waitFor();assert.deepEqual(prompts,[]);
  await page.locator('#imageDescriptionCancel').click();
  for(const viewport of [{width:320,height:568},{width:844,height:390},{width:390,height:320},{width:1280,height:900}]){
   await page.setViewportSize(viewport);await page.waitForFunction(()=>Math.abs(parseFloat(document.documentElement.style.getPropertyValue('--composer-height'))-innerHeight)<1);await page.evaluate(()=>openTransfer());
   const dialog=page.locator('#transferSendModal [role="dialog"]');
   await dialog.evaluate(e=>e.scrollTop=e.scrollHeight);const button=await page.locator('#transferSendConfirm').boundingBox();assert(button.y>=0&&button.y+button.height<=viewport.height,JSON.stringify({viewport,button}));
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await page.locator('#transferSendCancel').click();
  }
  assert.deepEqual(errors,[]);await page.close();
  // Full Moments page, real form submission and overlay stacking, fixture APIs.
  let html=fs.readFileSync(path.join(root,'templates/moments.html'),'utf8').replace(/\{%[\s\S]*?%\}/g,'').replace(/\{\{\s*user_timezone[^}]*\}\}/g,'"Asia/Shanghai"').replace(/\{\{\s*ui_language\s*\}\}/g,'zh-CN').replace(/\{\{[\s\S]*?\}\}/g,'').replace(/<script[^>]+src="[^"]*(?:auth-guard|ai-disclosure|ai-cruise|theme-loader|voice-call-incoming)[^"]*"[^>]*><\/script>/g,'').replace('<head>','<head><style>#theme-loading-screen{display:none!important;}</style><script src="/static/ui-language.js"></script><script src="/static/ai-disclosure.js"></script>');
  html=html.replace('</body>','<nav style="position:fixed;bottom:0;height:60px;width:100%;z-index:999;background:gray">Nav</nav></body>');
  const moment=await browser.newPage({viewport:{width:390,height:844}}),posted=[];let failPublish=true;
  moment.on('pageerror',e=>errors.push(e.message));moment.on('dialog',d=>d.accept());
  await moment.route('**/*',route=>{
   const pathname=new URL(route.request().url()).pathname,json=data=>route.fulfill({json:data});
   if(pathname==='/moments')return route.fulfill({contentType:'text/html',body:html});
   if(pathname.startsWith('/static/')){const p=path.join(root,pathname);return fs.existsSync(p)?route.fulfill({path:p}):route.fulfill({status:404,body:''});}
   if(pathname==='/api/vision/prompts')return json({prompts:['Saved instruction']});
   if(pathname==='/api/user/profile_settings')return json({ai_language:'zh'});
   if(pathname==='/api/moments/active_enabled')return json({enabled:false});
   if(pathname==='/api/moments/characters'||pathname==='/api/moments/related_characters')return json({});
   if(pathname==='/api/moments')return json([]);
   if(pathname==='/api/moments/post'){posted.push(route.request().postData());return failPublish?route.fulfill({status:502,json:{error:'Synthetic vision failure',code:'vision_failed',image_index:0}}):json({status:'success'});}
   throw new Error('Unexpected fixture request '+pathname);
  });
  await moment.goto('http://saki.test/moments');await moment.locator('#openMomentModal').click();
  const image=fs.readFileSync(path.join(root,'static/logo.png'));
  await moment.locator('#momentImageInput').setInputFiles([{name:'one.png',mimeType:'image/png',buffer:image},{name:'two.png',mimeType:'image/png',buffer:image}]);
  await moment.locator('#momentPromptPresets select option[value="Saved instruction"]').waitFor({state:'attached'});
  await moment.locator('#momentPromptPresets select').selectOption('Saved instruction');
  assert.equal(await moment.locator('#momentVisionPrompt').inputValue(),'Saved instruction');
  for(const viewport of [{width:320,height:568},{width:844,height:390},{width:390,height:320},{width:1280,height:900}]){
   await moment.setViewportSize(viewport);await moment.waitForFunction(()=>Math.abs(parseFloat(document.documentElement.style.getPropertyValue('--composer-height'))-innerHeight)<1);await moment.locator('#momentModal').evaluate(e=>e.scrollTop=e.scrollHeight);
   const result=await moment.locator('#momentComposeBtn').evaluate(e=>{const b=e.getBoundingClientRect();return {visible:b.y>=0&&b.bottom<=innerHeight,top:document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)===e};});
   assert(result.visible&&result.top,JSON.stringify({viewport,result}));assert(await moment.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await moment.setViewportSize({width:390,height:844});await moment.locator('#momentComposeBtn').click();
  await moment.waitForFunction(()=>!document.getElementById('momentComposeBtn').disabled);
  assert(posted[0].includes('Saved instruction'));assert.equal(await moment.locator('#momentVisionPrompt').inputValue(),'Saved instruction');assert(await moment.locator('#momentModalMask').isVisible());
  // Failure changes only the failed image to manual; shared instruction still applies to the other image.
  await moment.locator('.preview-description').first().fill('Manual picture');failPublish=false;await moment.locator('#momentComposeBtn').click();await moment.locator('#momentModalMask').waitFor({state:'hidden'});
  assert(posted[1].includes('Saved instruction'));assert.equal(await moment.locator('#momentVisionPrompt').inputValue(),'');assert.deepEqual(errors,[]);
  console.log('PASS: transfer send/cancel/failure/translation and accept/return captions; saved prompts reuse/delete; Moments shared prompt/failure retry; narrow/landscape/keyboard-height/desktop layout and bottom-nav stacking');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
