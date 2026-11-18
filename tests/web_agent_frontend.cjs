const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root,'templates/chat.html'), 'utf8');
const catalog = JSON.parse(fs.readFileSync(path.join(root,'static/locales/ui.json'),'utf8'));
function extract(name,next) { return source.slice(source.indexOf('  function '+name),source.indexOf('  function '+next)); }

test('browsing rows hide runtime instructions for all prompt languages and retain old history', () => {
    const sourceFunction = extract('extractCruiseFromContent','extractTicklesFromContent');
    const context = {}; vm.createContext(context); vm.runInContext(sourceFunction,context);
    const {buildPrompt} = require('../agent-test/agent_prompts');
    for (const lang of ['ja','en','zh-CN']) {
        const result = context.extractCruiseFromContent(buildPrompt({url:'https://example.org',text:'Page contents'},null,lang,'Spanish'));
        assert.equal(result.cleanText,'');
        assert.equal(result.cruises.length,1);
        assert.ok(result.cruises[0].summary.includes('Page contents'));
        assert.ok(!result.cruises[0].summary.includes('[ASK:'));
    }
    const old=context.extractCruiseFromContent('[WEB_CRUISE:https://example.org]\n【当前页面文本】old page\n请根据你的性格和当前页面instruction[/WEB_CRUISE]');
    assert.equal(old.cruises[0].summary,'old page');
});

test('new confirmation is translated for three interfaces', () => {
    const match=source.match(/confirm\(SakiI18n.ui`(让当前角色浏览[\s\S]*?)`\)/);
    assert.ok(match);
    const key=match[1].replace(/\$\{url\}/g,'{0}').replace(/\\n/g,'\n');
    for(const lang of ['ja','en','zh-CN']) assert.ok(catalog[key][lang].includes('{0}'));
});

test('start only succeeds on acknowledged HTTP response', async () => {
    const start=source.indexOf('  async function triggerAgent');
    const end=source.indexOf('  // --- 【Agent】停止函数',start);
    const alerts=[];
    const context={confirm:()=>true,alert:m=>alerts.push(m), charId:'hero',currentUserId:1,
        SakiI18n:{ui:()=>'',t:s=>s},console,
        updateAgentUI(){throw new Error('must not mark a failed start as running')},startAgentPolling(){},
        fetch:async()=>({ok:false,json:async()=>({status:'error',error:'busy'})})};
    vm.createContext(context);vm.runInContext(source.slice(start,end),context);
    await context.triggerAgent('https://example.org');
    assert.equal(alerts.length,1);
    assert.match(alerts[0],/busy/);
});
