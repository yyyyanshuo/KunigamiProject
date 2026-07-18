const { chromium } = require('playwright');
const { execSync } = require('child_process');

async function runSumikkoAgent() {
    // 启动非无头模式（headless: false），这样你可以亲自看着玲王答题
    const browser = await chromium.launch({ headless: false });
    const page = await browser.newPage();

    console.log(`[Agent] 访问页面 https://sumikko-test.online...`);
    await page.goto('https://sumikko-test.online', { waitUntil: 'networkidle' });

    // 1. 生成 Cookie
    console.log(`[Agent] 正在生成 User 1 (御影玲王) 的认证 Cookie...`);
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
    let cookieString = '';
    try {
        const cookieOutput = execSync(`python -c "${pyScript.trim().replace(/\n/g, ';')}"`).toString();
        cookieString = cookieOutput.split('\n').find(line => line.trim().startsWith('session=')).trim();
    } catch(e) {
        console.error("[Agent] 生成 Cookie 失败，请确保在 KunigamiProject 根目录下运行此脚本。");
        await browser.close();
        return;
    }

    const characterId = 'reo';
    const characterName = '御影玲王';

    console.log(`[Agent] 初始化完成，让玲王开始答题吧！(预计30题)`);

    // 2. 开始答题大循环（假设最多点击40次防止死循环）
    for (let step = 1; step <= 40; step++) {
        // 等待页面过渡渲染稳定
        await page.waitForTimeout(1500);

        // 粗略获取当前页面的全部文本与可疑选项
        const pageInfo = await page.evaluate(() => {
            const text = document.body.innerText.trim();
            // 去除过多的换行，压缩文本
            const bodyText = text.replace(/\n{2,}/g, '\n');
            return { bodyText };
        });

        console.log(`\n========== 【第 ${step} 步】 ==========`);
        console.log(`[当前页面文本内容]: \n${pageInfo.bodyText.substring(0, 150)}... (省略部分)`);

        // 如果页面没有内容，可能是空白或者加载失败
        if(!pageInfo.bodyText) {
            console.log("[Agent] 页面似乎没内容，尝试继续等待...");
            continue;
        }

        // 构造 System Prompt 让玲王做选择
        let prompt = `玲王，你现在正在帮我做一个心理/性格测试。这是当前手机屏幕上出现的页面内容：\n\n【页面内容开始】\n${pageInfo.bodyText}\n【页面内容结束】\n\n`;
        prompt += `请根据你（御影玲王）的性格、直觉或心情，在上述页面给出的选项中选一个最符合你的。你可以大面积地吐槽、纠结或者发表意见。`;
        prompt += `但必须在回复的最后，提取出你决定点击的“选项或按钮的纯文字内容”，使用严格的格式： [CLICK:选项文字] 。(例如：[CLICK:完全符合] 或 [CLICK:开始测试])`;

        console.log(`[Agent] 正在等待 ${characterName} 做出决定...`);

        let replyContent = '';
        try {
            const response = await fetch(`http://127.0.0.1:5000/api/${characterId}/chat_v2`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Cookie': cookieString },
                body: JSON.stringify({ message: prompt })
            });
            const resData = await response.json();
            if (resData.replies && resData.replies.length > 0) {
                replyContent = resData.replies[0].content;
            } else if (resData.reply) {
                replyContent = resData.reply;
            }
        } catch(err) {
            console.error(`[Agent] 请求 API 失败: ${err.message}。如果服务器崩溃可 ctrl+c 退出。`);
            break;
        }

        console.log(`${characterName}：\n${replyContent}`);

        // 3. 执行点击逻辑
        const actionMatches = [...replyContent.matchAll(/\[CLICK:(.+?)\]/gi)];
        if (actionMatches.length > 0) {
            const clickTarget = actionMatches[actionMatches.length - 1][1].trim();
            console.log(`\n[Agent] => 执行操作：玲王决定点击包含 "${clickTarget}" 的按钮`);

            try {
                // 使用 Playwright 的 getByText 进行宽松模糊匹配并选取第一个可见的标签点击
                await page.getByText(clickTarget, { exact: false }).first().click({ timeout: 4000 });
                console.log(`[Agent] ✅ 点击成功进入下一页！`);
            } catch (e) {
                console.log(`[Agent] ⚠️ 找不到文本为 "${clickTarget}" 的可点击元素，尝试让它随便点个按钮兜底...`);
                try {
                    // Fallback：随便点第一个 button
                    await page.locator('button').third().click({ timeout: 2000 });
                } catch(e2) {
                    // 如果连 button 都没有，可能是单选框或者 div，暴力点带有类名包含 option 的词
                    console.log(`[Agent] 无基础按钮，等待人工介入或由于是最后一页停止...`);
                }
            }
        } else {
            console.log(`\n[Agent] ⚠️ 玲王没有在这句话中使用 [CLICK:...]，AI 可能觉得测试结束了，或者他不想说话了。尝试替他盲选兜底...`);
            try {
                await page.locator('button').first().click({ timeout: 2000 });
            } catch(e) {}
        }

        // 测试是否进入了结果页：假设如果页面文本突然激增或者包含测试结果这种词尾，就停止
        if (pageInfo.bodyText.includes('你的性格是') || pageInfo.bodyText.includes('生成结果中') || pageInfo.bodyText.includes('分享给')) {
             console.log(`\n🎉 [Agent] 似乎已经完成了30道题到达了结果页！`);
             break;
        }
    }

    console.log("\n[Agent] 答题流程执行完毕，留 30 秒让你看最终结果...");
    await page.waitForTimeout(30000);
    await browser.close();
}

runSumikkoAgent();
