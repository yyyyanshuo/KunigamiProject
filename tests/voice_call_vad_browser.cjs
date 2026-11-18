// Real Silero V5 + ONNX WASM worker, public test speech and synthetic environmental noise.
// SAKI_VAD_TEST_WAV=/path/to/silero/tests/data/test.wav node tests/voice_call_vad_browser.cjs
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE||'playwright');
const root=path.join(__dirname,'..'), fixture=process.env.SAKI_VAD_TEST_WAV, production=process.env.SAKI_VAD_BASE_URL;
assert(fixture,'Set SAKI_VAD_TEST_WAV to a public speech fixture; do not use private recordings');
const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/'){res.setHeader('Content-Type','text/html');return res.end('<script src="/static/call-vad.js"></script><script src="/static/call-speech.js"></script>');}
  if(url.pathname==='/speech.wav'){res.setHeader('Content-Type','audio/wav');return res.end(fs.readFileSync(fixture));}
  if(!url.pathname.startsWith('/static/')){res.writeHead(404);return res.end();}
  const file=path.resolve(root,'.'+url.pathname);if(!file.startsWith(root+path.sep)){res.writeHead(403);return res.end();}
  try{res.setHeader('Content-Type',file.endsWith('.wasm')?'application/wasm':/\.m?js$/.test(file)?'application/javascript':'application/octet-stream');res.end(fs.readFileSync(file));}
  catch(_){res.writeHead(404);res.end();}
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true,executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE||undefined});
  try{
    const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    if(production){
      await page.goto(production+'/login');
      await page.addScriptTag({url:production+'/static/call-vad.js?v=20261030c'});
      await page.addScriptTag({url:production+'/static/call-speech.js?v=20261030c'});
    }else await page.goto(`http://127.0.0.1:${server.address().port}`);
    const result=await page.evaluate(async fixtureBase64=>{
      const context=new AudioContext({sampleRate:16000});
      const bytes=fixtureBase64?Uint8Array.from(atob(fixtureBase64),c=>c.charCodeAt(0)).buffer:await(await fetch('/speech.wav')).arrayBuffer();
      const decoded=await context.decodeAudioData(bytes);
      const speech=decoded.getChannelData(0).slice(0,16000*6);await context.close();
      const detector=new SakiCallSpeech.TurnDetector();let received, ready, failed;
      const frames=[];const started=performance.now();
      const vad=new SakiCallVAD.CallVoiceDetector({url:'/static/call-vad-worker.js?v=20261030c',
        onReady:()=>ready(),onError:()=>failed(new Error('VAD failed')),
        onFrames:(values)=>{frames.push(...values);for(const f of values)detector.voice(f.probability,f.at,f.duration);received?.();}});
      await new Promise((resolve,reject)=>{ready=resolve;failed=reject;});
      vad.reset(1);let time=0,seed=123;
      const noise=()=>{seed=(Math.imul(seed,1664525)+1013904223)|0;return ((seed>>>0)/4294967296-.5)*.025;};
      const push=async values=>{
        const pcm=Int16Array.from(values,x=>Math.round(Math.max(-1,Math.min(1,x))*32767));
        time+=100;detector.audio(pcm,time);await new Promise((resolve,reject)=>{received=resolve;failed=reject;vad.push(pcm,time);});
      };
      // Continuous fan-like hum, broadband room noise and sharp clicks exceed the old RMS threshold.
      for(let i=0;i<80;i++)await push(Float32Array.from({length:1600},(_,j)=>noise()+.02*Math.sin(2*Math.PI*120*(i*1600+j)/16000)+(i%15===0&&j<64?.4:0)));
      const idleActive=detector.active, idleRevision=detector.revision;frames.length=0;
      for(let i=0;i<speech.length;i+=1600)await push(speech.slice(i,i+1600));
      const speechFrames=frames.filter(f=>f.probability>=.65).length, lastSpeechAt=detector.lastVoiceAt;
      for(let i=0;i<80;i++)await push(Float32Array.from({length:1600},(_,j)=>noise()+.02*Math.sin(2*Math.PI*120*(i*1600+j)/16000)+(i%17===0&&j<64?.4:0)));
      const quietEnd=detector.endpoint(time), noiseExtension=detector.lastVoiceAt-lastSpeechAt;
      vad.close();return {idleActive,idleRevision,speechFrames,quietEnd,noiseExtension,elapsedMs:performance.now()-started};
    },production?fs.readFileSync(fixture).toString('base64'):null);
    assert.equal(result.idleActive,false);assert.equal(result.idleRevision,0);
    assert(result.speechFrames>20,'real speech must be detected');
    assert.equal(result.quietEnd,'silence');assert(result.noiseExtension<1000,'noise must not perpetually extend speech');
    assert.deepEqual(errors,[]);console.log('PASS real Silero VAD'+(production?' from production assets':'' )+':',JSON.stringify(result));
  }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
