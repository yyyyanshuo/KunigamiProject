const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../templates/call.html'), 'utf8');
const section = (start, end) => {
  const a=source.indexOf(start), b=source.indexOf(end,a);
  assert(a>=0&&b>a); return source.slice(a,b);
};
function setup() {
  const c=vm.createContext({
    now:0, Date:{now:()=>c.now}, busy:false, liveTextEl:{}, statusEl:{},
    setInterval(fn){c.tick=fn;return 1;}, clearInterval(){}, clearTimeout(){}, setTimeout(){},
    commitSpeech(state){c.commits++;}, commits:0,
  });
  vm.runInContext(section('  function realtimeSpokenText(', '  async function startListening()'),c);
  const state={committed:[],partial:'',stopped:false,finalizing:false,lastTextAt:null,lastRecognizedText:'',textRevision:0};
  c.listening=state;c.startSilenceWatch(state);
  return c;
}
const c=setup(), s=c.listening;
c.now=10000;c.tick();assert.equal(c.commits,0,'ambient sound without text must keep listening');
c.updateRealtimeTranscript(s,'partial_transcript','[noise]');
c.now=20000;c.tick();assert.equal(c.commits,0);
c.updateRealtimeTranscript(s,'partial_transcript','你好');
c.now=21999;c.tick();assert.equal(c.commits,0);
c.updateRealtimeTranscript(s,'partial_transcript','[noise]');
assert.equal(s.partial,'你好');
c.updateRealtimeTranscript(s,'partial_transcript','你好 [noise]');
c.updateRealtimeTranscript(s,'committed_transcript','你好');
assert.equal(s.lastTextAt,20000,'same partial/committed text and noise must not extend deadline');
c.now=22000;c.tick();assert.equal(c.commits,1);
const d=setup(), t=d.listening;
d.updateRealtimeTranscript(t,'partial_transcript','こんにちは');
d.now=1800;d.updateRealtimeTranscript(t,'partial_transcript','こんにちは元気ですか');
d.now=2000;d.tick();assert.equal(d.commits,0,'new words restart the two second wait');
d.now=3800;d.tick();assert.equal(d.commits,1);
t.stopped=true;d.tick();assert.equal(d.commits,1);
t.stopped=false;t.finalizing=true;d.tick();assert.equal(d.commits,1);

async function checkFinalization(newWords, hasSpeech=true) {
  const c=setup(), state=c.listening;
  Object.assign(c,{
    call:{status:'active'}, callId:'test', muted:false, ttsEnabled:false, Math,
    snapshotRecordedUtterance:async()=>({size:1}),
    transcribeRecordedUtterance:()=>new Promise(resolve=>c.resolveBatch=resolve),
    stopListening(){c.listening=null;state.stopped=true;},
    addTurn(turn){c.turns.push(turn);},turns:[],
    api:async()=>({}), startListening(){}, console,
  });
  vm.runInContext(section('  async function commitSpeech(state)', '  async function playSpeech(text)'),c);
  c.updateRealtimeTranscript(state,'partial_transcript','你好');
  const pending=c.commitSpeech(state);
  await new Promise(setImmediate);
  c.now=3000;
  c.updateRealtimeTranscript(state,'partial_transcript',newWords?'你好再等一下':'你好 [noise]');
  c.resolveBatch({text:hasSpeech?'你好':'',hasSpeech});
  await pending;
  if(newWords||!hasSpeech){
    assert.equal(c.turns.length,0);
    assert.equal(state.finalizing,false);
    assert.equal(c.busy,false);
    if(!hasSpeech)assert.equal(state.lastTextAt,null);
  }else{
    assert.equal(c.turns.length,1,'ambient noise must not cancel a completed utterance');
    assert.equal(c.turns[0].content,'你好');
    assert.equal(state.stopped,true);
  }
}
(async()=>{
  await checkFinalization(false);
  await checkFinalization(true);
  await checkFinalization(false,false);
  for(const m of source.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)){
    new vm.Script(m[1].replace(/\{\{[\s\S]*?\}\}/g,'1').replace(/\{%[\s\S]*?%\}/g,''));
  }
  console.log('PASS: two-second text inactivity, repeated/committed results, ambient-only input, continued speech, stop/finalization guards and batch-noise rejection');
})().catch(error=>{console.error(error);process.exitCode=1;});
