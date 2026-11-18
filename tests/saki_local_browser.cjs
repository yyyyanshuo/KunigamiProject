// Run against an initialized local server only. Credentials stay out of output.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.KUNIGAMI_PLAYWRIGHT_MODULE || 'playwright');
const config = Object.fromEntries(fs.readFileSync(path.join(__dirname, '../.env'), 'utf8')
  .split(/\r?\n/).filter(line => /^[A-Z_]+=/.test(line)).map(line => {
    const at = line.indexOf('='); return [line.slice(0, at), line.slice(at + 1)];
  }));
if (config.SAKI_LOCAL_TEST !== 'true') throw new Error('Local mode required');
const base = `http://127.0.0.1:${process.env.SAKI_PORT || config.SAKI_PORT || 5001}`;
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.KUNIGAMI_BROWSER_EXECUTABLE || undefined});
  try {
    const context = await browser.newContext();
    await context.route('**/*', route => route.request().url().startsWith(base + '/')
      ? route.continue() : route.abort());
    const page = await context.newPage();
    page.on('pageerror', error => console.error('PAGE ERROR:', error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(base + '/login');
    await page.locator('#username').fill(config.SAKI_TEST_EMAIL);
    await page.locator('#password').fill(config.SAKI_TEST_PASSWORD);
    await Promise.all([page.waitForURL(base + '/'), page.getByRole('button', {name: '登 录'}).click()]);
    await page.goto(base + '/chat/saki_demo');
    await page.waitForTimeout(1000);
    if (await page.locator('#chat-diagnostic-content').count()) throw new Error(await page.locator('#chat-diagnostic-content').textContent());
    await page.locator('#messageInput').fill('浏览器合成测试消息');
    await page.locator('#sendBtn').click();
    await page.getByText(/MOCK/).first().waitFor({timeout: 30000});
    await page.reload();
    await page.getByText('浏览器合成测试消息', {exact: false}).first().waitFor();
    assert((await context.cookies()).some(c => c.name === 'saki_global_session'));
    console.log('PASS browser login, chat send, MOCK reply, refresh persistence, isolated cookie');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.message); process.exitCode = 1;});
