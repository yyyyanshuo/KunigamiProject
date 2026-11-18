const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const Module=require('node:module');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'saki-agent-lifecycle-'));
process.env.SAKI_DATA_ROOT=root;
const {runUniversalAgent}=require('../agent-test/agent_universal');
const {messages}=require('../agent-test/agent_prompts');
test.after(()=>fs.rmSync(root,{recursive:true,force:true}));

for(const lang of ['ja','en','zh-CN']) {
  test(`task lifecycle keeps session out of browser and follows ${lang} with French replies`, async()=>{
    const originalLoad=Module._load, originalFetch=global.fetch;
    let closed=false, launchOptions, contextOptions, sent;
    const page={setDefaultTimeout(){},async goto(){},async waitForTimeout(){},async waitForLoadState(){},
      async evaluate(){return{url:'https://public.example',text:'Page content',inputs:'',buttons:''}}};
    const context={async route(){},async newPage(){return page},on(){}};
    const browser={async newContext(options){contextOptions=options;return context;},async close(){closed=true;}};
    Module._load=function(name,...args){
      if(name==='playwright')return{chromium:{async launch(options){launchOptions=options;return browser;}}};
      return originalLoad.call(this,name,...args);
    };
    global.fetch=async(url,options)=>{
      assert.equal(url,'http://127.0.0.1:8010/api/hero/chat_v2');
      assert.equal(options.headers.Cookie,'session=synthetic');
      assert.equal(options.redirect,'error');
      sent=JSON.parse(options.body).message;
      return{ok:true,async json(){return{full_reply:'Terminé. [FINISH]'}}};
    };
    try {
      await runUniversalAgent({user_id:1,char_id:'hero',url:'https://public.example',cookie:'session=synthetic',
        ui_language:lang,output_language:'French',proxy:'http://127.0.0.1:9999'});
      assert.ok(closed);
      assert.ok(sent.includes(messages(lang).language+' "French"'));
      assert.ok(!JSON.stringify(launchOptions).includes('synthetic'));
      assert.ok(!JSON.stringify(contextOptions).includes('synthetic'));
      assert.equal(launchOptions.proxy.bypass,'<-loopback>');
      assert.equal(contextOptions.serviceWorkers,'block');
      assert.equal(contextOptions.acceptDownloads,false);
      assert.equal(fs.existsSync(path.join(root,'users/1/agent_state.json')),false);
    }finally{Module._load=originalLoad;global.fetch=originalFetch;}
  });
}
