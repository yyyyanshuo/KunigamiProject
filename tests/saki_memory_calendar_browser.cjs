const assert=require('node:assert/strict');
const {chromium}=require(process.env.SAKI_PLAYWRIGHT_MODULE || '../agent-test/node_modules/playwright');
const base='http://127.0.0.1:5098';
(async()=>{const browser=await chromium.launch({headless:true});try{
const c=await browser.newContext({viewport:{width:390,height:844},serviceWorkers:'block'});
await c.route('**/*',r=>r.request().url().startsWith(base+'/')?r.continue():r.abort());
await c.request.post(base+'/api/login',{data:{username:'test@saki.invalid',password:'synthetic-mobile-review-password'}});
await c.request.post(base+'/api/ui/language',{data:{user_language:'en'}});
assert.equal((await c.request.post(base+'/api/user/profile_settings',{data:{timezone:'America/Los_Angeles'}})).status(),200);
const page=await c.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.clock.install({time:new Date('2026-09-24T00:00:00Z')});
const response=await page.goto(base+'/memory/saki_demo'); assert.equal(response.status(),200);
assert.equal(await page.evaluate(()=>beijingDateString(new Date('2026-09-24T00:00:00Z'))),'2026-09-24');
assert.equal(await page.evaluate(()=>shiftBeijingDate('2026-09-23',-1)),'2026-09-22');
await page.route('**/api/moments?*',r=>r.fulfill({json:[{char_id:'user',remark:'User',timestamp:'2026-09-24 01:30:00',content:'Time fixture',liker_ids:[],comments:[]}]}));
await page.goto(base+'/moments');await page.locator('.moment-card').waitFor();
assert.match(await page.locator('.moment-card').textContent(),/Today 10:30/);
await page.goto(base+'/chat/saki_demo');await page.waitForTimeout(300);
const t=await page.evaluate(()=>{
const el=renderMessageGroup({id:9990,role:'assistant',timestamp:'2026-09-24 01:30:00',content:'time fixture'});
return el.querySelector('.timestamp').textContent;
});assert.match(t,/10:30/);
assert.deepEqual(errors,[]);console.log('PASS user timezone for chat/Moments and Beijing memory calendar; day shifts retain date');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
