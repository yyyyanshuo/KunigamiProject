const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const browser = await chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe', headless:true});
  const html = fs.readFileSync(path.join(__dirname, '../scripts/contacts-diagnostic.html'), 'utf8');
  try {
    for (const test of [
      {name:'ahead', pending:[{type:'chat',id:'private-character-id',last_message_id:999}], expected:'待同步已读 ID 超过服务器最新消息 ID：1'},
      {name:'normal', pending:[], expected:'本轮未检出上述异常'},
      {name:'invalid', pending:[{type:'chat',id:'private-character-id',last_message_id:'bad'}], expected:'待同步记录格式异常：1'},
      {name:'render', pending:[], badField:true, expected:'可能导致页面渲染失败的字段类型数量：1'},
      {name:'unauthorized', pending:[], status:401, expected:'接口未成功，本次停止'},
    ]) {
      const context = await browser.newContext({viewport:{width:390,height:844}, isMobile:true});
      const sent = [];
      await context.addInitScript(items => {
        localStorage.setItem('unrelated-key', 'keep-this');
        items.forEach((item,i) => localStorage.setItem('kunigami_pending_read:1:chat:' + i, JSON.stringify(item)));
        localStorage.setItem('kunigami_pending_read:2:chat:other-user', 'do-not-inspect');
        window.storageBeforeDiagnostic = JSON.stringify(Object.entries(localStorage).sort());
      }, test.pending);
      await context.route('https://diagnostic.invalid/**', async route => {
        const req = route.request();
        sent.push(req.method());
        const pathname = new URL(req.url()).pathname;
        if (pathname === '/static/contacts-diagnostic.html') return route.fulfill({contentType:'text/html',body:html});
        if (pathname === '/api/contacts') return route.fulfill({status:test.status || 200,contentType:'application/json',body:JSON.stringify([{id:'private-character-id',type:'chat',last_message_id:10,remark:test.badField ? 42 : 'private-name',last_msg:'private-message',last_time:'16:00'}])});
        if (pathname === '/') return route.fulfill({contentType:'text/html',body:'<script>if (await ReadReceipts.flush(1, data)) {}</script>'});
        throw new Error('Unexpected request: '+pathname);
      });
      const page = await context.newPage();
      await page.goto('https://diagnostic.invalid/static/contacts-diagnostic.html');
      await page.waitForFunction(() => window.diagnosticFinished === true);
      const text = await page.locator('#report').innerText();
      assert.ok(text.includes(test.expected), text);
      for (const secret of ['private-character-id','private-name','private-message','do-not-inspect']) assert.ok(!text.includes(secret));
      assert.ok(sent.every(method => method === 'GET'));
      assert.equal(await page.evaluate(() => JSON.stringify(Object.entries(localStorage).sort()) === window.storageBeforeDiagnostic), true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await context.close();
      process.stdout.write('PASS '+test.name+'\n');
    }
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode=1;});
