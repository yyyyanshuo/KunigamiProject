const assert=require('node:assert/strict');
const {chromium}=require('C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--disable-background-networking','--disable-sync','--no-first-run']});
 try {
  const page=await browser.newPage();await page.route('**/*',route=>route.abort());
  await page.setContent('<html lang="en"><body><button>今日は学校です</button><div data-model-output id="model">今日は学校です</div><div data-model-output><code id="code">今日は学校です</code></div><textarea id="editor">今日は学校です</textarea><div data-model-output id="stream">今日は学校です</div></body></html>');
  await page.evaluate(()=>{window.SakiFuriganaEnabled=true;window.requests=[];window.fetch=async(url,options)=>{const text=JSON.parse(options.body).text;requests.push(text);await new Promise(r=>setTimeout(r,60));return {ok:true,json:async()=>({result:text.replace('学校','<ruby>学校<rt>がっこう</rt></ruby>')})};};});
  await page.addScriptTag({path:'static/model-furigana.js'});
  await page.evaluate(()=>document.getElementById('stream').textContent='明日は学校です');
  await page.waitForFunction(()=>document.querySelector('#model ruby')&&document.querySelector('#stream ruby'));
  assert.equal(await page.locator('button ruby').count(),0);assert.equal(await page.locator('#code ruby').count(),0);
  assert.equal(await page.locator('#editor').inputValue(),'今日は学校です');
  assert.equal(await page.evaluate(()=>SakiModelDisplay.plain(document.getElementById('stream'))),'明日は学校です');
  assert.equal(await page.evaluate(()=>{const range=document.createRange();range.selectNodeContents(document.getElementById('model'));const selection=getSelection();selection.removeAllRanges();selection.addRange(range);const data=new DataTransfer();document.dispatchEvent(new ClipboardEvent('copy',{clipboardData:data,bubbles:true,cancelable:true}));return data.getData('text/plain');}),'今日は学校です');
  await page.evaluate(()=>SakiModelDisplay.preview(document.getElementById('editor')));
  await page.waitForFunction(()=>document.querySelector('#editor + [data-model-output] ruby'));
  await page.locator('#editor').fill('edited draft');assert.equal(await page.locator('#editor + [data-model-output]').count(),0);
  console.log('PASS real Chrome: model-only annotation, protected code and inputs, streaming race, plain copy, editable raw preview');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
