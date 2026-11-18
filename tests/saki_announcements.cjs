const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../templates/tabbar.html'), 'utf8');
const code = html.slice(html.indexOf('    function localDateKey()'), html.indexOf('    // 页面加载完成后检查弹窗'));
async function run(payload, storage, fail=false) {
  const elements = Object.fromEntries(['daily-announcement-modal','voice-announcement-section','voice-announcement-cta','voice-announcement-delete-date','persona-lock-announcement-section'].map(id => [id,{style:{display:'none'}}]));
  const ctx = vm.createContext({Date, console:{warn(){}}, document:{getElementById:id=>elements[id]},localStorage:{getItem:k=>storage[k],setItem:(k,v)=>storage[k]=v},fetch:async()=>{if(fail)throw Error('offline');return {ok:true,json:async()=>payload}}});
  vm.runInContext(code,ctx);
  await ctx.checkDailyAnnouncementModal();
  return elements['daily-announcement-modal'].style.display;
}
(async()=>{
 const seen={};
 assert.equal(await run({announcements:[],server_date:'2026-09-22'},seen),'none');
 const payload={announcements:[{id:'test',kind:'persona_lock',active:true}],server_date:'2026-09-22'};
 assert.equal(await run(payload,seen),'flex');
 assert.equal(await run(payload,seen),'none');
 assert.equal(await run({...payload,server_date:'2026-09-23'},seen),'flex');
 assert.equal(await run(payload,{},true),'none');
 console.log('PASS announcements: absent, first visit, repeat visit, next day, fetch failure');
})().catch(e=>{console.error(e);process.exitCode=1});
