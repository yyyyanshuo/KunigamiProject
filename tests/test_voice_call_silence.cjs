// Run with: node --test tests/test_voice_call_silence.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const {TurnDetector, hasSpeech, wavBlob} = require('../static/call-speech.js');
const {CallPCMResampler} = require('../static/call-audio-worklet.js');
const pipelineSource = fs.readFileSync(path.join(__dirname, '../static/call-speech.js'), 'utf8');
const loud = () => new Int16Array(1600).fill(6000);
const quiet = () => new Int16Array(1600);

function harness(options = {}) {
  const timers = new Map(), sockets = [], turns = [], errors = [], statuses = [];
  let now = 0, nextTimer = 0, batches = 0, vadProbability;
  class FakeVAD {
    constructor(callbacks) { this.callbacks=callbacks;this.closed=false;this.pending=new Map();Promise.resolve().then(()=>callbacks.onReady()); }
    reset() { this.pending.clear(); }
    push(pcm,at) { this.callbacks.onFrames([{probability:vadProbability??(pcm[0]===6000?.98:.01),at,duration:100}],at); }
    close() { this.closed=true; }
  }
  class Socket {
    constructor(url) { this.url = new URL(url); this.readyState = 0; this.bufferedAmount = 0; this.sent = []; sockets.push(this); }
    send(text) { this.sent.push(JSON.parse(text)); }
    close() { this.readyState = 3; }
    message(message) { this.onmessage?.({data:JSON.stringify(message)}); }
    ready() { this.readyState = 1; this.message({message_type:'session_started'}); }
  }
  const timer = (fn, ms, repeat = false) => { const id=++nextTimer; timers.set(id,{fn,ms,repeat,at:now+ms}); return id; };
  const sandbox = {
    module:{exports:{}}, console, URL, Blob, AbortController, DOMException, Int16Array, Uint8Array, ArrayBuffer, DataView,
    performance:{now:()=>now}, Date, Math, WebSocket:Socket, SakiCallVAD:{CallVoiceDetector:FakeVAD},
    btoa:text=>Buffer.from(text,'binary').toString('base64'),
    setTimeout:(fn,ms)=>timer(fn,ms), clearTimeout:id=>timers.delete(id),
    setInterval:(fn,ms)=>timer(fn,ms,true), clearInterval:id=>timers.delete(id),
    ...options.globals,
  };
  vm.runInNewContext(pipelineSource, sandbox);
  const input = new sandbox.module.exports.CallSpeechInput({
    workletUrl:'/static/call-audio-worklet.js',
    fetchToken:async()=>({token:'test-only'}),
    transcribe:async(blob,signal)=>{batches++; return options.transcribe ? options.transcribe(blob,signal) : {text:'补救识别',hasSpeech:true};},
    onTurn:turn=>turns.push(turn), onError:(error,retry)=>errors.push({error,retry}), onStatus:status=>statuses.push(status),
    ...options.pipeline,
  });
  const flush = async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
  return {
    input,sockets,turns,errors,statuses,timers,sandbox,flush,
    get batches(){return batches;},
    async start() { input.stream={getTracks:()=>[]}; input.startVAD(); await flush(); input.resume(); await flush(); sockets.at(-1)?.ready(); },
    async advance(ms) {
      const end=now+ms;
      for (;;) {
        const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
        if (!due) break;
        const [id,t]=due;now=t.at;
        if(t.repeat)t.at+=t.ms;else timers.delete(id);
        t.fn();await flush();
      }
      now=end;await flush();
    },
    async frame(pcm=loud(),probability) { vadProbability=probability;await this.advance(100); input.audio(pcm); await flush(); },
    async speak() { await this.frame();await this.frame(); },
  };
}

async function silence(h, ms=3000) { for(let i=0;i<ms/100;i++) await h.frame(quiet()); }

test('committed preview never sends; all turns wait 3s then re-recognize full recording',async()=>{
  const h=harness();await h.start();
  assert.equal(h.sockets[0].url.searchParams.get('vad_silence_threshold_secs'),'1.5');
  await h.speak();await h.speak();
  h.sockets[0].message({message_type:'committed_transcript',text:'错误的实时文字'});
  await silence(h,2900);assert.equal(h.turns.length,0);assert.equal(h.batches,0);
  await silence(h,100);assert.equal(h.batches,1);assert.equal(h.turns[0].text,'补救识别');
  assert.equal(h.turns[0].metrics.source,'batch_recording');
  await h.advance(4000);assert.equal(h.turns.length,1);
});

test('one and two second pauses, finals and punctuation do not split the utterance',async()=>{
  const h=harness();await h.start();await h.speak();
  for(const pause of [1000,2000]){
    h.sockets[0].message({message_type:'committed_transcript',text:'没说完'});
    await silence(h,pause);assert.equal(h.batches,0);await h.speak();
  }
  await silence(h);assert.equal(h.turns.length,1);assert.equal(h.batches,1);
  const t=new TurnDetector();t.audio(loud(),100);t.voice(.99,100,200);t.transcript('partial_transcript','你好',300);
  t.transcript('partial_transcript','你好。 [noise]',3000);
  assert.equal(t.lastTextAt,300);assert.equal(t.endpoint(3299),null);assert.equal(t.endpoint(3300),'silence');
});

test('continued speech cancels snapshot, retains full audio and ignores old result',async()=>{
  const requests=[];
  const h=harness({transcribe:(blob,signal)=>new Promise(resolve=>requests.push({blob,signal,resolve}))});
  await h.start();await h.speak();await silence(h);
  const id=h.input.turnId;assert.equal(requests.length,1);
  const size=requests[0].blob.size;
  await h.speak();assert(requests[0].signal.aborted);assert.equal(h.input.enabled,true);
  requests[0].resolve({text:'过时的前半句',hasSpeech:true});await h.flush();assert.equal(h.turns.length,0);
  await silence(h);assert.equal(requests.length,2);assert(requests[1].blob.size>size+32000);
  requests[1].resolve({text:'完整的前后两句',hasSpeech:true});await h.flush();
  assert.equal(h.turns[0].clientTurnId,id);assert.equal(h.turns[0].text,'完整的前后两句');
});

test('caption corrections during recording ASR cannot cause retry loops',async()=>{
  let resolve,signal;
  const h=harness({transcribe:(_b,s)=>{signal=s;return new Promise(r=>resolve=r);}});
  await h.start();await h.speak();await silence(h);
  h.sockets[0].message({message_type:'committed_transcript',text:'迟到的字幕修正'});
  assert.equal(signal.aborted,false);assert.equal(h.batches,1);
  resolve({text:'录音结果',hasSpeech:true});await h.flush();assert.equal(h.turns[0].text,'录音结果');
});

test('zero realtime text and socket interruption still send whole-recording recognition',async()=>{
  for(const disconnect of [false,true]){
    const h=harness();await h.start();await h.speak();
    if(disconnect){h.sockets[0].message({message_type:'partial_transcript',text:'只识别到开头'});h.sockets[0].onclose();}
    await h.speak();await silence(h);assert.equal(h.batches,1);assert.equal(h.turns[0].text,'补救识别');
  }
});

test('microphone is reused across four recording-based turns',async()=>{
  const h=harness();await h.start();
  for(let i=0;i<4;i++){
    await h.speak();await silence(h);assert.equal(h.turns.length,i+1);assert.equal(h.input.enabled,false);
    await h.input.start();await h.flush();h.sockets.at(-1).ready();
    assert.equal(h.input.enabled,true);assert.equal(h.input.starting,null);
  }
  h.input.close();
});

test('idle buffers bounded; recording limit requires manual send instead of splitting speech',async()=>{
  const t=new TurnDetector();for(let i=0;i<36000;i++)t.audio(quiet(),i*100);
  assert.equal(t.endpoint(3600000),null);assert.equal(t.frames.length,0);assert.equal(t.preRoll.length,100);
  const h=harness({pipeline:{maxTurnMs:1000}});await h.start();
  for(let i=0;i<20;i++)await h.speak();
  assert.equal(h.batches,0);assert(h.input.recordingFull);assert(h.statuses.includes('limit'));
  assert.equal(h.input.turn.frames.length,10);
  await h.input.commitNow();assert.equal(h.turns.length,1);
});

test('soft onset preserved in pre-roll; Unicode preview starts recording even below threshold',()=>{
  for(const text of ['你好','こんにちは','مرحبا','Привет','안녕'])assert.equal(hasSpeech(text),true);
  assert.equal(hasSpeech('（背景嘈杂）[noise]'),false);
  const t=new TurnDetector();for(let i=0;i<30;i++)t.audio(new Int16Array(1600).fill(30),i*100);
  t.transcript('partial_transcript','مرحبا',3000);
  assert.equal(t.frames.length,30);assert.equal(t.frames[0][0],30);
  assert.equal(t.endpoint(5999),null);assert.equal(t.endpoint(6000),'silence');
  const soft=new TurnDetector();soft.audio(new Int16Array(1600).fill(150),100);soft.voice(.98,100,200);assert(soft.active);
});

test('manual send works with no preview and no detected onset',async()=>{
  const h=harness();await h.start();await h.frame(quiet());
  assert.equal(h.input.turn.active,false);await h.input.commitNow();
  assert.equal(h.batches,1);assert.equal(h.turns[0].text,'补救识别');
});

test('idle socket failures retry twice while recording continues independently',async()=>{
  const h=harness();await h.start();h.sockets[0].onclose();
  await h.advance(500);assert.equal(h.sockets.length,2);
  h.sockets[1].ready();h.sockets[1].onclose();await h.advance(1000);assert.equal(h.sockets.length,3);
  h.sockets[2].ready();h.sockets[2].onclose();await h.advance(1500);
  assert.equal(h.sockets.length,3);assert.equal(h.input.previewUnavailable,true);h.input.close();
});

test('token timeout cannot lose recording or accumulate unlimited audio queue',async()=>{
  const h=harness({pipeline:{fetchToken:()=>new Promise(()=>{}),connectTimeoutMs:400}});
  h.input.stream={getTracks:()=>[]};h.input.startVAD();await h.flush();h.input.resume();await h.speak();await h.advance(500);
  assert.equal(h.input.previewUnavailable,true);assert.equal(h.input.queue.length,0);
  await silence(h);assert.equal(h.turns[0].text,'补救识别');h.input.close();
});

test('ASR timeout retains recording; retry succeeds without ever using preview text',async()=>{
  let resolve,signal;
  const h=harness({transcribe:(_b,s)=>{signal=s;return new Promise(r=>resolve=r);},pipeline:{batchTimeoutMs:500}});
  await h.start();await h.speak();h.sockets[0].message({message_type:'committed_transcript',text:'不能发送的实时字幕'});
  await silence(h);await silence(h,500);
  assert.equal(h.errors.length,1);assert.equal(h.errors[0].retry,true);assert(signal.aborted);
  const id=h.input.pending.clientTurnId;
  resolve({text:'迟到的结果',hasSpeech:true});await h.flush();await silence(h,1000);
  assert.equal(h.turns.length,0);assert.equal(h.batches,1);
  h.input.options.transcribe=async()=>({text:'录音重试结果',hasSpeech:true});await h.input.retry();
  assert.equal(h.turns[0].clientTurnId,id);assert.equal(h.turns[0].text,'录音重试结果');
});

test('empty ASR retains audio for retry/continuation and does not loop or use preview',async()=>{
  const h=harness({transcribe:async()=>({text:'[noise]',hasSpeech:false})});
  await h.start();await h.speak();h.sockets[0].message({message_type:'committed_transcript',text:'不可靠字幕'});
  await silence(h);const size=h.input.pending.blob.size;await silence(h,1000);
  assert.equal(h.batches,1);assert.equal(h.turns.length,0);assert(h.statuses.includes('empty'));
  assert(h.input.turn.frames.length);h.input.options.transcribe=async blob=>{
    assert(blob.size>size);return{text:'继续说后的完整录音',hasSpeech:true};
  };
  await h.speak();await silence(h);assert.equal(h.turns[0].text,'继续说后的完整录音');
});

test('mute/hangup aborts in-flight ASR, ignores late text and releases timers',async()=>{
  for(const action of ['pause','close']){
    let resolve,signal;const h=harness({transcribe:(_b,s)=>{signal=s;return new Promise(r=>resolve=r);}});
    await h.start();await h.speak();await silence(h);h.input[action]();
    assert(signal.aborted);resolve({text:'不能发送',hasSpeech:true});await h.flush();
    assert.equal(h.turns.length,0);assert.equal(h.errors.length,0);assert.equal(h.timers.size,0);
  }
});

test('canceling pending microphone permission releases late stream',async()=>{
  let resolve,stops=0;
  const h=harness({globals:{navigator:{mediaDevices:{getUserMedia:()=>new Promise(r=>resolve=r)}}}});
  const starting=h.input.start();h.input.close();resolve({getTracks:()=>[{stop:()=>stops++}]});await starting;
  assert.equal(stops,1);assert.equal(h.input.stream,null);assert.equal(h.input.enabled,false);
});

test('PCM resampling and dual-rate worklet preserve audio at 16k/44.1k/48k/96k',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../static/call-audio-worklet.js'),'utf8');
  for(const rate of [16000,44100,48000,96000]){
    let Processor;const messages=[];
    vm.runInNewContext(source,{sampleRate:rate,Int16Array,AudioWorkletProcessor:class{
      constructor(){this.port={postMessage:m=>messages.push(m)};}
    },registerProcessor:(_name,p)=>Processor=p});
    const p=new Processor();
    for(let offset=0;offset<rate*2;offset+=128)p.process([[new Float32Array(Math.min(128,rate*2-offset)).fill(0.25)]]);
    assert.equal(messages.length,20);const recordingRate=Math.min(rate,48000);
    assert(messages.every(m=>m.pcm.length===1600&&m.recording.length===recordingRate/10));
    assert(messages.every(m=>m.pcm.every(v=>v===8192)&&m.recording.every(v=>v===8192)));
    const wav=await wavBlob(messages.map(m=>m.recording),recordingRate).arrayBuffer(),v=new DataView(wav);
    assert.equal(wav.byteLength,44+recordingRate*4);assert.equal(v.getUint32(24,true),recordingRate);
    assert.equal(v.getUint32(28,true),recordingRate*2);assert.equal(v.getInt16(44,true),8192);
  }
});

test('controller uploads native-rate recording with correct WAV metadata',async()=>{
  let data;
  const h=harness({transcribe:async blob=>{data=await blob.arrayBuffer();return{text:'高清录音',hasSpeech:true};}});
  await h.start();h.input.audio({pcm:loud(),recording:new Int16Array(4800).fill(1234),recordingSampleRate:48000});
  await h.input.commitNow();const v=new DataView(data);
  assert.equal(v.getUint32(24,true),48000);assert.equal(v.getUint32(40,true),9600);assert.equal(v.getInt16(44,true),1234);
});

test('template inline scripts parse after input pipeline changes',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../templates/call.html'),'utf8');
  for(const m of source.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))
    new vm.Script(m[1].replace(/\{\{[\s\S]*?\}\}/g,'1').replace(/\{%[\s\S]*?%\}/g,''));
});


test('loud non-speech cannot start a turn or perpetually extend the end of speech',async()=>{
  const h=harness();await h.start();
  for(let i=0;i<50;i++)await h.frame(loud(),.01);
  assert.equal(h.batches,0);assert.equal(h.input.turn.active,false);
  await h.speak();
  for(let i=0;i<30;i++)await h.frame(loud(),.01);
  assert.equal(h.batches,1);assert.equal(h.turns.length,1);
});

test('room noise and one high-probability click during ASR do not cancel the request',async()=>{
  let finish,signal;const h=harness({transcribe:(_b,s)=>{signal=s;return new Promise(r=>finish=r);}});
  await h.start();await h.speak();await silence(h);
  for(let i=0;i<30;i++)await h.frame(loud(),i===5?.99:.01);
  assert.equal(signal.aborted,false);assert.equal(h.batches,1);
  finish({text:'完整录音识别',hasSpeech:true});await h.flush();
  assert.equal(h.turns[0].text,'完整录音识别');assert.equal(h.statuses.filter(s=>s==='transcribing').length,1);
});

test('confirmed speech during ASR still cancels while tentative noise expires',async()=>{
  let resolve,signal;const h=harness({transcribe:(_b,s)=>{signal=s;return new Promise(r=>resolve=r);}});
  await h.start();await h.speak();await silence(h);
  await h.frame(loud(),.99);assert.equal(signal.aborted,false);
  await h.frame(loud(),.99);assert.equal(signal.aborted,true);
  resolve({text:'过时结果',hasSpeech:true});await h.flush();assert.equal(h.turns.length,0);h.input.close();
});

test('explicit send is a commitment even if speech or noise continues afterward',async()=>{
  let resolve;const h=harness({transcribe:()=>new Promise(r=>resolve=r)});
  await h.start();await h.speak();const request=h.input.commitNow();await h.flush();
  const signal=h.input.pending.controller.signal;await h.speak();assert.equal(signal.aborted,false);
  resolve({text:'手动提交的录音',hasSpeech:true});await request;
  assert.equal(h.turns[0].text,'手动提交的录音');
});

test('VAD failure disables automatic detection but manual recording recognition still works',async()=>{
  const h=harness();await h.start();h.input.vad.callbacks.onError();
  for(let i=0;i<20;i++)await h.frame(loud(),.01);
  h.sockets[0].message({message_type:'committed_transcript',text:'只作预览'});await silence(h,4000);
  assert.equal(h.batches,0);assert(h.statuses.includes('manual'));
  await h.input.commitNow();assert.equal(h.turns[0].text,'补救识别');
});


test('separated high-probability clicks cannot accumulate into confirmed speech',()=>{
  const t=new TurnDetector();t.audio(loud(),100);
  for(let i=0;i<100;i++)t.voice(i%3===0?.99:.5,i*32,32);
  assert.equal(t.revision,0);assert.equal(t.active,false);
});

test('VAD worker bridge drops old turn replies and bounds a stalled inference queue',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../static/call-vad.js'),'utf8');
  let worker, failures=0,results=0,terminated=0;
  const sandbox={module:{exports:{}},setTimeout:()=>1,clearTimeout:()=>{},queueMicrotask:fn=>fn(),Worker:class{
    constructor(){worker=this;this.messages=[];}postMessage(m){this.messages.push(m);}terminate(){terminated++;}
  }};
  vm.runInNewContext(source,sandbox);
  const vad=new sandbox.module.exports.CallVoiceDetector({url:'/worker',onFrames:()=>results++,onError:()=>failures++});
  worker.onmessage({data:{type:'ready'}});vad.reset(1);vad.push(loud(),100);vad.reset(2);
  worker.onmessage({data:{type:'result',epoch:1,id:1,frames:[],through:100}});assert.equal(results,0);
  vad.push(loud(),200);worker.onmessage({data:{type:'result',epoch:2,id:2,frames:[],through:200}});assert.equal(results,1);
  for(let i=0;i<11;i++)vad.push(loud(),300+i*100);
  assert.equal(failures,1);assert.equal(terminated,1);assert.equal(vad.pending.size,0);assert.equal(vad.closed,true);
  worker.onmessage({data:{type:'ready'}});assert.equal(vad.ready,false);
});

test('a delayed recording response survives the former 25 second cutoff and sends once',async()=>{
  let finish;const h=harness({transcribe:()=>new Promise(r=>finish=r)});
  await h.start();await h.speak();await silence(h);
  await silence(h,30000);assert.equal(h.errors.length,0);assert.equal(h.batches,1);
  finish({text:'慢速网络下的完整录音',hasSpeech:true});await h.flush();
  assert.equal(h.turns.length,1);assert.equal(h.turns[0].text,'慢速网络下的完整录音');
});

test('retry reuses the failed snapshot byte for byte after a long noisy wait',async()=>{
  const blobs=[];const h=harness({transcribe:async blob=>{blobs.push(blob);throw new Error('upload failed');}});
  await h.start();await h.speak();await silence(h);const id=h.input.turnId;
  await silence(h,12000);await h.input.retry();
  assert.equal(blobs[1],blobs[0]);assert.equal(h.input.turnId,id);
  const size=h.input.turn.frames.length;await h.speak();await silence(h,5000);
  assert.equal(h.input.turn.frames.length,size);assert.equal(h.batches,2);
  h.input.options.transcribe=async blob=>{assert.equal(blob,blobs[0]);return{text:'同一份录音',hasSpeech:true};};
  await h.input.retry();assert.equal(h.turns.length,1);assert.equal(h.turns[0].clientTurnId,id);
});

test('manual send during automatic recognition commits the existing request without duplicate upload',async()=>{
  let finish;const h=harness({transcribe:()=>new Promise(r=>finish=r)});
  await h.start();await h.speak();await silence(h);const pending=h.input.pending;
  await h.input.commitNow();await h.speak();assert.equal(h.batches,1);assert.equal(pending.controller.signal.aborted,false);
  finish({text:'手动确认当前录音',hasSpeech:true});await h.flush();assert.equal(h.turns.length,1);
});

function uploadHarness() {
  let xhr;const h=harness({globals:{FormData,XMLHttpRequest:class {
    constructor(){xhr=this;this.upload={};this.headers={};}
    open(method,url){this.method=method;this.url=url;}
    setRequestHeader(k,v){this.headers[k]=v;}
    send(form){this.form=form;}
    abort(){this.aborted=true;this.onabort?.();}
  }}});
  const controller=new AbortController(),progress=[],results=[],errors=[];
  const promise=h.sandbox.module.exports.uploadRecording('/transcribe',new Blob(['recording']),controller.signal,
    (...args)=>progress.push(args)).then(r=>results.push(r),e=>errors.push(e));
  return {h,get xhr(){return xhr;},controller,progress,results,errors,promise};
}

test('upload and recognition have independent budgets with progress and a valid result',async()=>{
  const t=uploadHarness();await t.h.advance(45000);assert.equal(t.errors.length,0);
  t.xhr.upload.onprogress({lengthComputable:true,loaded:3,total:4});assert.equal(t.progress.at(-1)[1],75);
  t.xhr.upload.onload();await t.h.advance(45000);assert.equal(t.errors.length,0);
  t.xhr.status=200;t.xhr.response={transcript:'已识别',has_speech:true};t.xhr.onload();await t.promise;
  assert.equal(t.results[0].text,'已识别');assert.equal(t.h.timers.size,0);
  assert.equal(t.xhr.headers['X-Requested-With'],'XMLHttpRequest');
});

test('upload timeout, recognition timeout and provider failure are distinct and clean up requests',async()=>{
  for(const kind of ['upload','recognition','provider','invalid','cancel']){
    const t=uploadHarness();
    if(kind==='upload')await t.h.advance(60000);
    if(kind==='recognition'){t.xhr.upload.onload();await t.h.advance(55000);}
    if(kind==='provider'){t.xhr.status=504;t.xhr.response={error:'录音识别超时',code:'call_recognition_timeout'};t.xhr.onload();}
    if(kind==='invalid'){t.xhr.status=200;t.xhr.response=null;t.xhr.onload();}
    if(kind==='cancel')t.controller.abort();
    await t.promise;assert.equal(t.errors.length,1);assert.equal(t.results.length,0);assert.equal(t.h.timers.size,0);
    if(kind==='upload')assert.equal(t.errors[0].code,'call_upload_timeout');
    if(['recognition','provider'].includes(kind))assert.equal(t.errors[0].code,'call_recognition_timeout');
    if(kind==='invalid')assert.equal(t.errors[0].code,'call_transcript_invalid');
    if(kind==='cancel')assert.equal(t.errors[0].name,'AbortError');
    t.xhr.status=200;t.xhr.response={transcript:'迟到结果',has_speech:true};t.xhr.onload();assert.equal(t.results.length,0);
  }
});

test('service worker leaves only same-origin call recording POSTs to the browser',()=>{
  const listeners={};let intercepted=0;
  const sandbox={URL,importScripts:()=>{},caches:{},self:{location:{origin:'https://example.test'},addEventListener:(k,fn)=>listeners[k]=fn},fetch:async()=>({})};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/sw.js'),'utf8'),sandbox);
  for(const [method,url,expected] of [
    ['POST','https://example.test/api/calls/call_abc/transcribe',0],
    ['GET','https://example.test/api/calls/call_abc/transcribe',1],
    ['POST','https://example.test/api/calls/call_abc/turn',1],
    ['POST','https://other.test/api/calls/call_abc/transcribe',1],
  ]){intercepted=0;listeners.fetch({request:{method,url},respondWith:()=>intercepted++});assert.equal(intercepted,expected);}
});
