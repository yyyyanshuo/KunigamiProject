// Isolated offline admin page rendering; no production users or paid API requests.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE||'C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const base=process.env.SAKI_BROWSER_URL||'http://127.0.0.1:5098';
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{
  const context=await browser.newContext({baseURL:base,viewport:{width:1280,height:900},serviceWorkers:'block'});
  await context.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  assert.equal((await context.request.post('/api/login',{data:{username:'test@saki.invalid',password:'synthetic-browser-password'}})).status(),200);
  await context.addInitScript(()=>{window.chartOptions=[];window.echarts={init:()=>({setOption:o=>window.chartOptions.push(o),resize:()=>{}})};});
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/admin/stats',r=>r.fulfill({json:{
   users:{total:2,new_today:1,dau:2,wau:2,retention_1d:50,retention_7d:20,avg_streak:3},
   messages:{total:10,avg_per_user:5,user_sent:5,ai_sent:5,ai_proactive:1,daily_sessions:2,single_chat:6,group_chat:4},
   characters:{total:2,avg_per_user:1,top_10:[{char_id:'synthetic-character',count:10}]},groups:{total:1},moments:{user_posts:1,ai_posts:1}
  }}));
  await page.route('**/api/admin/circuit_status',r=>r.fulfill({json:{users:[
   {user_id:7,email:'synthetic@tests.invalid',is_frozen:0,has_disabled_route:true,breakers:[{route:'openai',status_code:401,count:3,route_disabled:1}]},
   {user_id:8,email:'second@tests.invalid',is_frozen:0,has_disabled_route:false,breakers:[{route:'custom',status_code:403,count:2,cooldown_until:new Date(Date.now()+600000).toISOString()}]}
  ]}}));
  for(const language of ['zh-CN','en','ja']){
   await context.request.post('/api/ui/language',{data:{user_language:language}});
   const response=await page.goto('/admin/dashboard');assert.equal(response.status(),200);
   await page.waitForFunction(()=>window.chartOptions.length===2&&document.querySelectorAll('#table-circuit-users tr').length===2);
   assert.equal(await page.locator('html').getAttribute('lang'),language);
   const texts=await page.locator('body').innerText();
   const chartLabels=await page.evaluate(()=>chartOptions.flatMap(o=>o.series.flatMap(s=>[s.name,...s.data.map(d=>d.name)])));
   if(language==='en'){
    assert(!/[\u4e00-\u9fff]/.test(texts),texts);
    assert(chartLabels.every(text=>!/[\u4e00-\u9fff]/.test(text)),chartLabels);
   }
   if(language==='ja')assert(chartLabels.includes('メッセージの種類'));
   assert.equal(await page.locator('#impersonate-user-id').getAttribute('min'),'1');
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: admin dashboard in 3 languages, chart labels, cooldown/disabled states, ID input');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
