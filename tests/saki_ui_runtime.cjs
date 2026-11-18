const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const root=path.join(__dirname,'..');
const catalog=JSON.parse(fs.readFileSync(path.join(root,'static/locales/ui.json'),'utf8'));
const chatSource=fs.readFileSync(path.join(root,'templates/chat.html'),'utf8');
const safetyNotice=chatSource.slice(chatSource.indexOf('  function appendSafetyAlertRow(group)'),chatSource.indexOf('  function parseTransferMessageSegment(text)'));
// Region-specific numbers verified against NHC, MHLW, 988lifeline.org and 988.ca.
const crisisResources={
 'zh-CN':{phone:'12356',region:'中国大陆',localHelp:'其他地区请联系当地危机援助热线'},
 en:{phone:'988',region:'U.S. or Canada',localHelp:'Elsewhere, contact your local crisis helpline'},
 ja:{phone:'0120-061-338',region:'日本では',localHelp:'日本以外では、お住まいの地域の相談窓口'},
};
let count=0;
function walk(dir){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const f=path.join(dir,e.name);if(e.isDirectory()){walk(f);continue;}if(!f.endsWith('.html'))continue;const source=fs.readFileSync(f,'utf8').replace(/<!--[\s\S]*?-->/g,'');for(const m of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)){const js=m[1].replace(/{{[\s\S]*?}}/g,'0').replace(/{%[\s\S]*?%}/g,'');try{new vm.Script(js,{filename:path.relative(root,f)});count++;}catch(err){throw err;}}}}
walk(path.join(root,'templates'));
for(const lang of ['en','ja','zh-CN']){
 const ctx=vm.createContext({navigator:{},document:{documentElement:{lang}},window:{SakiUICatalog:Object.fromEntries(Object.entries(catalog).map(([k,v])=>[k,v[lang]]))}});
 vm.runInContext(fs.readFileSync(path.join(root,'static/ui-language.js'),'utf8'),ctx);
 const t=ctx.window.SakiI18n;
 assert.equal(t.t('用户语言'),catalog['用户语言'][lang]);
 const raw='用户语言 <span>记忆</span> {1}';
 assert.equal(t.ui(['',''],raw),raw);
 assert.equal(t.fragment('<b>用户语言</b>'),'<b>'+catalog['用户语言'][lang]+'</b>');
 assert(t.ui(['已选择 ',' 条'],3).includes('3'));
 assert.equal(t.ui(['<button class="','">卡片编辑</button>'],'active'),'<button class="active">'+catalog['卡片编辑'][lang]+'</button>');
 assert.equal(t.ui(['<div class="','">','</div>'],'test',raw),'<div class="test">'+raw+'</div>');
 assert.equal(t.needsSendTranslation(lang),false);
 assert.equal(t.needsSendTranslation({en:'English',ja:'日本語','zh-CN':'zh'}[lang]),false);
 assert.equal(t.needsSendTranslation(lang==='en'?'ja':'en'),true);
 // Exercise the chat renderer with the real UI catalog, independently of model language.
 ctx.SakiI18n=t;
 ctx.aiLanguage=lang==='en'?'ja':'en';
 ctx.document.createElement=tag=>({tagName:tag});
 vm.runInContext(safetyNotice,ctx);
 const group={children:[],appendChild(row){this.children.push(row);}};
 ctx.appendSafetyAlertRow(group);
 assert.equal(group.children.length,1);
 const notice=group.children[0];
 assert.equal(notice.className,'safety-alert-row');
 const resource=crisisResources[lang];
 assert(notice.textContent.includes(resource.phone),lang+' must show its crisis hotline');
 assert(notice.textContent.includes(resource.region),lang+' must name the service region');
 assert(notice.textContent.includes(resource.localHelp),lang+' must cover users in other regions');
 for(const [otherLanguage,otherResource] of Object.entries(crisisResources)){
  if(otherLanguage!==lang) assert(!notice.textContent.includes(otherResource.phone),lang+' must not show '+otherLanguage+' hotline');
 }
}
console.log('PASS '+count+' inline scripts compile; three-language runtime and regional crisis notices; interpolation preserves raw user content');
