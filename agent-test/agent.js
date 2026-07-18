const { chromium } = require('playwright');

async function runAgent() {

    // 打开浏览器
    const browser = await chromium.launch({
        headless: false
    });

    const page = await browser.newPage();

    // 访问网页
    await page.goto('http://localhost:3000/index.html');

    // 读取内容
    const text = await page.locator('#content').innerText();

    // 模拟角色
    const characterName = '御影玲王';

    // 输出结果
    console.log(`${characterName}：${text}`);

    await browser.close();
}

runAgent();