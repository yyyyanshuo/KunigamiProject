// Isolated browser regression: real AudioWorklet + synthetic microphone, mocked STT/LLM.
// KUNIGAMI_PLAYWRIGHT_MODULE=/path/to/playwright node tests/voice_call_pipeline_browser.cjs
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const assert=require('node:assert/strict');
const {chromium}=require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE||'playwright');
const root=path.join(__dirname,'..');
const fixture=process.env.SAKI_VAD_TEST_WAV;assert(fixture,'Set SAKI_VAD_TEST_WAV to the public Silero speech fixture');
const template=fs.readFileSync(path.join(root,'templates/call.html'),'utf8')
  .replace(/\{%[\s\S]*?%\}/g,'').replace(/\{\{\s*call_id\|tojson\s*\}\}/g,'"test-call"').replace(/\{\{[\s\S]*?\}\}/g,'en')
  .replace('<head>','<head><script>window.SakiI18n={t:s=>s,ui:(s,...v)=>s.reduce((a,b,i)=>a+b+(v[i]??""),"")};</script>');
let batches=0, turnAttempts=[], failNext=false, hangBatch=false, heldBatch, failBatch=false, emptyBatch=false;
const wavs=[];
const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://localhost');
  const chunks=[];for await(const chunk of req)chunks.push(chunk);
  const body=Buffer.concat(chunks);
  const json=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
  if(url.pathname==='/call') {res.setHeader('Content-Type','text/html');return res.end(template);}
  if(url.pathname==='/static/auth-guard.js'){res.setHeader('Content-Type','application/javascript');return res.end('');}
  if(url.pathname==='/speech.wav'){res.setHeader('Content-Type','audio/wav');return res.end(fs.readFileSync(fixture));}
  if(['/static/call-speech.js','/static/call-audio-worklet.js','/static/call-vad.js','/static/call-vad-worker.js','/static/ai-disclosure.css'].includes(url.pathname)||url.pathname.startsWith('/static/vendor/call-vad-20261030/')){
    res.setHeader('Content-Type',url.pathname.endsWith('.css')?'text/css':url.pathname.endsWith('.wasm')?'application/wasm':'application/javascript');
    return res.end(fs.readFileSync(path.join(root,url.pathname)));
  }
  if(url.pathname==='/api/calls/test-call')return json({call:{status:'active',tts_enabled:false,char_id:'test',turns:[],character:{name:'Test',avatar:'/avatar'}}});
  if(url.pathname.endsWith('/stt-token'))return json({token:'browser-test-only'});
  if(url.pathname.endsWith('/transcribe')){
    batches++;const offset=body.indexOf(Buffer.from('RIFF'));assert(offset>=0);
    const bytes=body.readUInt32LE(offset+4)+8, wav=body.subarray(offset,offset+bytes);wavs.push(wav);
    assert.equal(wav.subarray(8,12).toString(),'WAVE');assert(wav.readUInt32LE(24)>=44100);
    assert.equal(wav.readUInt32LE(40),wav.length-44);
    if(failBatch){failBatch=false;return json({error:'模拟录音识别失败'},503);}
    if(emptyBatch){emptyBatch=false;return json({transcript:'',has_speech:false});}
    if(hangBatch){heldBatch=()=>json({transcript:'迟到的补救',has_speech:true});return;}
    return json({transcript:'音频补救成功',has_speech:true});
  }
  if(url.pathname.endsWith('/turn')){
    const turn=JSON.parse(body);turnAttempts.push(turn);
    if(failNext){failNext=false;return json({error:'模拟发送失败'},503);}
    return json({assistant_turn:{id:`reply-${turn.client_turn_id}`,role:'assistant',content:'已收到'}});
  }
  if(url.pathname.endsWith('/end'))return json({call:{status:'ended',end_reason:'hangup'}});
  if(url.pathname.endsWith('/heartbeat'))return json({});
  res.writeHead(404);res.end();
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required'],executablePath:process.env.KUNIGAMI_BROWSER_EXECUTABLE||undefined});
  try{
    const page=await browser.newPage();const pageErrors=[],sockets=[];
    page.on('pageerror',error=>pageErrors.push(error.message));
    await page.route('**/*',route=>route.request().url().startsWith(base)?route.continue():route.abort());
    await page.routeWebSocket('wss://api.elevenlabs.io/**',socket=>{
      sockets.push(socket);socket.onMessage(data=>{
        const message=JSON.parse(data);assert.equal(message.sample_rate,16000);
        assert.equal(Buffer.from(message.audio_base_64,'base64').length,3200);
      });
      socket.send(JSON.stringify({message_type:'session_started'}));
    });
    await page.addInitScript(()=>{
      window.micRequests=0;
      navigator.mediaDevices.getUserMedia=async()=>{
        window.micRequests++;
        const context=new AudioContext(),gain=context.createGain(),dest=context.createMediaStreamDestination();
        gain.connect(dest);gain.gain.value=1;await context.resume();
        const speech=await context.decodeAudioData(await(await fetch('/speech.wav')).arrayBuffer());
        const noise=context.createBuffer(1,context.sampleRate*2,context.sampleRate),data=noise.getChannelData(0);
        let seed=123;for(let i=0;i<data.length;i++){seed=(Math.imul(seed,1664525)+1013904223)|0;data[i]=((seed>>>0)/4294967296-.5)*.025+.02*Math.sin(2*Math.PI*120*i/context.sampleRate);}
        const background=context.createBufferSource(),noiseGain=context.createGain();background.buffer=noise;background.loop=true;
        background.connect(noiseGain);noiseGain.connect(dest);noiseGain.gain.value=0;background.start();
        window.setNoise=()=>{noiseGain.gain.value=1;};
        window.bump=()=>{const source=context.createBufferSource(),buffer=context.createBuffer(1,context.sampleRate/100,context.sampleRate);buffer.getChannelData(0).fill(.4);source.buffer=buffer;source.connect(dest);source.start();};
        window.speak=()=>{const source=context.createBufferSource();source.buffer=speech;source.connect(gain);source.start(0,0,1.5);};
        window.testMic=dest.stream;
        return dest.stream;
      };
    });
    const waitListening=()=>page.waitForFunction(()=>document.getElementById('status').textContent==='正在聆听');
    const speak=()=>page.evaluate(()=>window.speak());
    const preview=text=>sockets.at(-1).send(JSON.stringify({message_type:'committed_transcript',text}));
    const waitReplies=count=>page.waitForFunction(n=>document.querySelectorAll('.turn.assistant').length===n,count,{timeout:8000});
    await page.goto(base+'/call');await waitListening();
    await page.locator('#sendSpeechBtn:not(.hidden)').waitFor(); // Even without any preview.
    await speak();await page.waitForTimeout(800);preview('错误的实时字幕');await page.evaluate(()=>window.setNoise());
    await page.waitForTimeout(1400);assert.equal(batches,0);assert.equal(turnAttempts.length,0);
    await speak();await page.waitForTimeout(700);preview('错误的后半句');
    await page.waitForTimeout(1500);assert.equal(turnAttempts.length,0);
    await waitReplies(1);assert.equal(batches,1);assert.equal(turnAttempts[0].text,'音频补救成功');
    assert(wavs[0].length>44100*2*5,'both halves and hesitation must be recorded');
    await waitListening();assert.equal(await page.evaluate(()=>window.micRequests),1);

    // Failed turn delivery retries exactly the recognized text and same ID, no second bubble/ASR.
    failNext=true;await speak();await page.waitForTimeout(300);preview('实时结果不能发送');
    await page.locator('#retrySpeechBtn:not(.hidden)').waitFor({timeout:7000});
    const failedId=turnAttempts.at(-1).client_turn_id, previousBatches=batches;
    await page.locator('#retrySpeechBtn').click();await waitReplies(2);
    assert.equal(turnAttempts.at(-1).client_turn_id,failedId);assert.equal(batches,previousBatches);
    assert.equal(await page.locator('.turn.user').count(),2);

    // Speaking during ASR cancels old snapshot, then re-recognizes the full accumulated recording.
    await waitListening();hangBatch=true;await speak();
    await page.waitForFunction(()=>document.getElementById('status').textContent==='正在识别整句话…');
    for(let i=0;i<20&&!heldBatch;i++)await page.waitForTimeout(50);
    assert(heldBatch);
    const noiseBatchCount=batches;await page.evaluate(()=>window.bump());await page.waitForTimeout(700);
    assert.equal(batches,noiseBatchCount);assert.equal(await page.locator('#status').textContent(),'正在识别整句话…');
    const finishOld=heldBatch;heldBatch=null;
    const beforeContinue=turnAttempts.length, firstSize=wavs.at(-1).length;
    hangBatch=false;await speak();await page.waitForTimeout(400);finishOld();
    await page.waitForTimeout(500);assert.equal(turnAttempts.length,beforeContinue);
    await waitReplies(3);assert(wavs.at(-1).length>firstSize+44100*2);
    assert.equal(turnAttempts.at(-1).text,'音频补救成功');

    // No preview + empty/failing ASR keeps recording and offers retry; never sends preview.
    await waitListening();emptyBatch=true;await speak();
    await page.locator('#retrySpeechBtn:not(.hidden)').waitFor({timeout:7000});
    const beforeRetry=turnAttempts.length;
    assert.equal(beforeRetry,beforeContinue+1);failBatch=true;
    await page.locator('#retrySpeechBtn').click();
    await page.locator('#retrySpeechBtn:not(.hidden)').waitFor({timeout:3000});
    assert.equal(turnAttempts.length,beforeRetry);
    await page.locator('#retrySpeechBtn').click();await waitReplies(4);
    assert.equal(turnAttempts.at(-1).text,'音频补救成功');

    // Clicking Send fixes this recording; retry sends those same bytes once.
    await waitListening();failBatch=true;await speak();await page.waitForTimeout(500);
    await page.locator('#sendSpeechBtn').click();
    await page.locator('#retrySpeechBtn:not(.hidden)').waitFor({timeout:3000});
    const manualWav=wavs.at(-1),beforeManualRetry=turnAttempts.length;
    await page.waitForTimeout(1200);await page.locator('#retrySpeechBtn').click();await waitReplies(5);
    assert(wavs.at(-1).equals(manualWav));assert.equal(turnAttempts.length,beforeManualRetry+1);

    await waitListening();hangBatch=true;await speak();
    await page.waitForFunction(()=>document.getElementById('status').textContent==='正在识别整句话…');
    for(let i=0;i<20&&!heldBatch;i++)await page.waitForTimeout(50);
    assert(heldBatch);const count=turnAttempts.length;
    await page.locator('#hangupBtn').click();heldBatch();await page.waitForTimeout(400);
    assert.equal(turnAttempts.length,count);assert.equal(await page.evaluate(()=>window.testMic.getTracks()[0].readyState),'ended');
    assert.equal(await page.locator('#status').textContent(),'通话已结束');assert.deepEqual(pageErrors,[]);
    console.log('PASS browser: real Silero with ongoing room noise and clicks, native-rate recording ASR every turn, 3s pause, continuation during ASR, no preview submission, empty/failure retry, one microphone, stable turn ID, hangup cancellation');
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
