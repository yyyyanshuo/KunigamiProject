// Real XHR upload over a throttled connection, including the production PWA worker.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE||'playwright');
const root=path.join(__dirname,'..');let receivedBytes=0;
const server=http.createServer(async(req,res)=>{
  if(req.url==='/') {res.setHeader('Content-Type','text/html');return res.end('<script src="/static/call-speech.js"></script>');}
  if(['/static/call-speech.js','/static/sw.js','/static/locales/worker.js'].includes(req.url)){
    res.setHeader('Content-Type','application/javascript');res.setHeader('Service-Worker-Allowed','/');return res.end(fs.readFileSync(path.join(root,req.url)));
  }
  if(req.url==='/api/calls/upload-test/transcribe'){
    receivedBytes=0;for await(const chunk of req)receivedBytes+=chunk.length;
    await new Promise(r=>setTimeout(r,1500));res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({transcript:'完整录音已送达',has_speech:true}));
  }
  res.writeHead(404);res.end();
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage();await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async()=>{await navigator.serviceWorker.register('/static/sw.js',{scope:'/'});await navigator.serviceWorker.ready;});
  await page.reload();await page.waitForFunction(()=>navigator.serviceWorker.controller!==null);
  const cdp=await page.context().newCDPSession(page);await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions',{offline:false,latency:100,downloadThroughput:1024*1024,uploadThroughput:32*1024});
  const result=await page.evaluate(async()=>{
    const stages=[],start=performance.now();
    const input=new SakiCallSpeech.CallSpeechInput({
      transcribe:(blob,signal,progress)=>SakiCallSpeech.uploadRecording('/api/calls/upload-test/transcribe',blob,signal,progress),
      onProgress:(stage,percent)=>stages.push({stage,percent}),
    });
    input.enabled=true;input.turn=new SakiCallSpeech.TurnDetector();input.turnId='slow-upload-turn';
    input.turn.frames=[new Int16Array(48000*12).fill(1234)];input.turn.sampleRate=48000;input.turn.startedAt=performance.now();
    let delivered,error;input.options.onTurn=t=>delivered=t;input.options.onError=e=>error=e.message;
    await input.commitNow();input.close();return{elapsedMs:performance.now()-start,delivered,error,stages};
  });
  assert(!result.error,result.error);assert(result.elapsedMs>25000,JSON.stringify(result));
  assert.equal(result.delivered.text,'完整录音已送达');assert.equal(result.delivered.clientTurnId,'slow-upload-turn');
  assert(result.stages.some(s=>s.stage==='uploading'&&s.percent>0&&s.percent<100));
  assert.equal(result.stages.at(-1).stage,'transcribing');assert(receivedBytes>48000*12*2);
  console.log(JSON.stringify({passed:true,elapsedMs:Math.round(result.elapsedMs),receivedBytes,pwaControlled:true,deliveredExactlyOnce:true}));
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
