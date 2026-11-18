const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/read-receipts.js'), 'utf8');
function harness(options = {}) {
  const storage = new Map(), requests = [];
  const context = {window:{}, console:{warn(){}}, AbortController, setTimeout, clearTimeout,
    localStorage:{getItem:k=>storage.get(k) ?? null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k),key:i=>[...storage.keys()][i],get length(){return storage.size;}},
    handleApiUnauthorized:()=>false,
    fetchWithAuthGuard:async (url,init) => {
      const body=JSON.parse(init.body); requests.push(body);
      if(options.during) options.during(context.window.ReadReceipts);
      if(options.networkError) throw new Error('synthetic network failure');
      return {ok:!options.fail,status:options.fail?500:200,json:async()=>options.fail?{error:'synthetic failure'}:{status:'success',conversations:(body.conversations||[body]).map(item=>({type:item.type,id:item.id,last_read_id:options.lowAck?0:item.last_message_id}))}};
    }
  };
  vm.runInNewContext(source,context);
  return {api:context.window.ReadReceipts,storage,requests};
}
const contact=(id='a',last=10,type='chat')=>({id,type,last_message_id:last,unread:5});
const item=(id='a',last=5,type='chat')=>({id,type,last_message_id:last});
let count=0;
async function test(name, run){await run();count++;console.log('PASS '+name);}
(async()=>{
 await test('ahead cursor retained without request or marking newer messages read',async()=>{
   const h=harness();h.api.queue(1,item('a',99));const before=[...h.storage.entries()];const c=contact();
   assert.equal(await h.api.flush(1,[c]),false);assert.equal(h.requests.length,0);assert.deepEqual([...h.storage.entries()],before);assert.equal(c.unread,5);
 });
 await test('valid receipts still sync alongside stale ones',async()=>{
   const h=harness();h.api.queue(1,item('a',99));h.api.queue(1,item('b',5));
   assert.equal(await h.api.flush(1,[contact(),contact('b')]),true);
   assert.deepEqual(h.requests[0].conversations,[item('b',5)]);
   assert.equal(h.api.pending(1).length,1);assert.equal(h.api.pending(1)[0].last_message_id,99);
 });
 await test('retry HTTP failure does not reject contacts loading',async()=>{
   const h=harness({fail:true});h.api.queue(1,item());
   assert.equal(await h.api.flush(1,[contact()]),false);assert.equal(h.api.pending(1).length,1);
 });
 await test('retry network failure preserves queue',async()=>{
   const h=harness({networkError:true});h.api.queue(1,item());
   assert.equal(await h.api.flush(1,[contact()]),false);assert.equal(h.api.pending(1).length,1);
 });
 await test('insufficient confirmation never clears a pending receipt',async()=>{
   const h=harness({lowAck:true});h.api.queue(1,item());
   assert.equal(await h.api.flush(1,[contact()]),false);assert.equal(h.api.pending(1).length,1);
 });
 await test('explicit read actions still report failures',async()=>{
   const h=harness({fail:true});
   await assert.rejects(h.api.saveBatch(1,[item()]),/已读状态保存失败/);
   await assert.rejects(h.api.send(1,item()),/已读状态保存失败/);
   assert.equal(h.api.pending(1).length,1);
 });
 await test('new receipt queued in flight is not removed',async()=>{
   const h=harness({during:api=>api.queue(1,item('a',8))});h.api.queue(1,item('a',5));
   assert.equal(await h.api.flush(1,[contact()]),true);assert.equal(h.api.pending(1)[0].last_message_id,8);
 });
 await test('conversation types and accounts remain isolated',async()=>{
   const h=harness();h.api.queue(1,item('a',99));h.api.queue(1,item('a',5,'group'));h.api.queue(2,item('a',5));
   await h.api.flush(1,[contact(),contact('a',10,'group')]);
   assert.deepEqual(h.requests[0].conversations,[item('a',5,'group')]);assert.equal(h.api.pending(2).length,1);
 });
 await test('empty current history does not cause stale receipt retries',async()=>{
   const h=harness();h.api.queue(1,item());assert.equal(await h.api.flush(1,[contact('a',0)]),false);assert.equal(h.requests.length,0);assert.equal(h.api.pending(1).length,1);
 });
 await test('existing deleted-conversation cleanup remains unchanged',async()=>{
   const h=harness();h.api.queue(1,item());assert.equal(await h.api.flush(1,[]),false);assert.equal(h.api.pending(1).length,0);assert.equal(h.requests.length,0);
 });
 console.log(count+' tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
