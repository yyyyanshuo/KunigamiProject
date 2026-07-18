const { chromium } = require('playwright');
const { execSync } = require('child_process');

async function runAgent() {
    // 1. 启动并访问本地静态页面
    const browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();

    console.log(`[Agent] 访问本地页面 http://localhost:3000/index.html...`);
    try {
        await page.goto('http://localhost:3000/index.html');
    } catch (e) {
        console.error(`[Agent] 无法访问本地页面，请确保 agent-test/server.js 正在运行 (node agent-test/server.js)`);
        await browser.close();
        return;
    }

    const text = await page.locator('#content').innerText();
    console.log(`[Agent] 成功读取页面内容: "${text}"\n`);

    // 2. 模拟角色获取认证 Cookie (用户1)
    console.log(`[Agent] 正在生成 User 1 的认证 Cookie...`);
    const pyScript = `
import sys, os
sys.path.insert(0, os.path.dirname(os.getcwd()))
from app import app
from flask.sessions import SecureCookieSessionInterface
si = SecureCookieSessionInterface()
s = si.session_class()
s['user_id'] = 1
s['logged_in'] = True
val = si.get_signing_serializer(app).dumps(dict(s))
print(f'session={val}')
`;
    // 过滤可能产生的无关日志，只抓取 session= 开头的行
    const cookieOutput = execSync(`python -c "${pyScript.trim().replace(/\n/g, ';')}"`).toString();
    const cookieString = cookieOutput.split('\n').find(line => line.trim().startsWith('session=')).trim();

    // 3. 调用 KunigamiProject 聊天 API
    const characterId = 'reo';
    const characterName = '御影玲王';
    console.log(`[Agent] 将页面内容发送给 ${characterName} 的 API...\n`);

    // 调用 chat_v2 接口
    const apiUrl = `http://127.0.0.1:5000/api/${characterId}/chat_v2`;

    try {
        const response = await fetch(apiUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Cookie': cookieString
            },
            body: JSON.stringify({ message: text }) // 发送选按钮的问题
        });

        const resData = await response.json();

        console.log(`=========================================`);
        // 解析角色回复
        let replyContent = '';
        if (resData.replies && resData.replies.length > 0) {
            replyContent = resData.replies[0].content;
        } else if (resData.reply) {
            replyContent = resData.reply;
        } else if (resData.error) {
            console.error(`[Error] 接口返回错误: ${resData.error}`);
        } else {
            console.log(`[Response] ${JSON.stringify(resData)}`);
        }

        if (replyContent) {
            console.log(`${characterName}：${replyContent}`);

            // Agent 模拟点击网页按钮
            // 提取所有 [CLICK:X] 的操作指令
            const actionMatches = [...replyContent.matchAll(/\[CLICK:([AB])\]/gi)];

            if (actionMatches.length > 0) {
                // 取最后一个匹配项，防止他反复横跳（先思考B又临时改成A）
                const finalAction = actionMatches[actionMatches.length - 1][1].toUpperCase();
                console.log(`\n[Agent] 解析到明确操作指令：执行了 [CLICK:${finalAction}]`);
                if (finalAction === 'A') {
                    await page.click('#btnA');
                } else {
                    await page.click('#btnB');
                }
            } else {
                console.log(`\n[Agent] 玲王没有使用严格的 [CLICK:X] 格式。转入 fallback 分析（查找最后倾向）...`);
                // 找到最后一次出现的 A 或 B 相关词汇的索引，以最后出现的为准
                const lastA = Math.max(replyContent.toUpperCase().lastIndexOf('A'), replyContent.lastIndexOf('国神'), replyContent.lastIndexOf('炼介'));
                const lastB = Math.max(replyContent.toUpperCase().lastIndexOf('B'), replyContent.lastIndexOf('凪'), replyContent.lastIndexOf('诚士郎'));

                if (lastA > lastB) {
                    console.log(`\n[Agent] Fallback: 根据上下文最后的语境，检测到玲王最终倾向于 A...`);
                    await page.click('#btnA');
                } else if (lastB > lastA) {
                    console.log(`\n[Agent] Fallback: 根据上下文最后的语境，检测到玲王最终倾向于 B...`);
                    await page.click('#btnB');
                } else {
                    console.log(`\n[Agent] 无法分辨最终选择，默认帮按 B 吧...`);
                    await page.click('#btnB');
                }
            }

            // 验证点击结果
            const resultText = await page.locator('#result').innerText();
            console.log(`[Agent] 返回结果: ${resultText}`);
        }
        console.log(`=========================================`);

    } catch (err) {
        console.error(`\n[Agent] API 调用失败！请确保 KunigamiProject 的 app.py 已运行。`);
        console.error(err.message);
    }

    await browser.close();
}

runAgent();