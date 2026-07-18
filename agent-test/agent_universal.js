const { chromium } = require('playwright');
const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

/**
 * Universal Agent for KunigamiProject
 * 使角色的 AI 能够像真人一样"操纵"浏览器：点击、输入、滚动、导航。
 * 支持 [ASK] 提问暂停、[WAIT] 自由对话暂停、用户 [STOP] 停止。
 */

// ==================== 配置 ====================
const BASE_URL = 'http://127.0.0.1:8000';
const ROOT_DIR = process.env.KUNIGAMI_ROOT || '/var/www/kunigami';
const POLL_INTERVAL_MS = 2000;
const MAX_STEPS = 50;

// ==================== IPC 文件操作 ====================

function getUsersRoot(userId) {
    return path.join(ROOT_DIR, 'users', String(userId));
}

function getStatePath(userId) {
    return path.join(getUsersRoot(userId), 'agent_state.json');
}

function getInputPath(userId) {
    return path.join(getUsersRoot(userId), 'agent_input.json');
}

function readJsonFile(filePath) {
    try {
        if (!fs.existsSync(filePath)) return null;
        const data = fs.readFileSync(filePath, 'utf-8');
        return JSON.parse(data);
    } catch (e) {
        return null;
    }
}

function writeJsonFile(filePath, data) {
    try {
        const dir = path.dirname(filePath);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(filePath, JSON.stringify(data, null, 2), 'utf-8');
    } catch (e) {
        console.warn(`[IPC] 写入文件失败: ${filePath}`, e.message);
    }
}

function consumeInput(userId) {
    const inputPath = getInputPath(userId);
    const data = readJsonFile(inputPath);
    if (data && data.command) {
        try { fs.unlinkSync(inputPath); } catch (e) {}
        return data;
    }
    return null;
}

function updateState(userId, state) {
    const statePath = getStatePath(userId);
    const current = readJsonFile(statePath) || {};
    const merged = Object.assign({}, current, state, { last_activity: new Date().toISOString() });
    writeJsonFile(statePath, merged);
}

function cleanupState(userId) {
    try { fs.unlinkSync(getStatePath(userId)); } catch (e) {}
    try { fs.unlinkSync(getInputPath(userId)); } catch (e) {}
}

// ==================== API 调用 ====================

async function chatViaCharApi(snapshot, charId, userId, cookieString) {
    // 用 WEB_CRUISE 包裹页面快照，作为 user 消息发给 chat_v2
    // chat_v2 自带人设加载 + 存库，前端自动渲染"正在阅读"cruise-row
    let msg = `[WEB_CRUISE:${snapshot.url}]\n【当前页面文本】\n${snapshot.text}\n`;
    if (snapshot.inputs) msg += `\n【可用的输入框】\n${snapshot.inputs}\n`;
    if (snapshot.buttons) msg += `\n【可点击的按钮/链接】\n${snapshot.buttons}\n`;
    msg += `\n请根据你的性格和当前页面内容，自然地和用户交谈，并在回复末尾附带网页操作指令:\n`;
    msg += `1. [CLICK:文本或选择器] - 点击页面元素\n`;
    msg += `2. [TYPE:输入框描述|文本内容] - 在输入框输入内容\n`;
    msg += `3. [GOTO:URL] - 跳转到新网址\n`;
    msg += `4. [BACK] - 返回上一页\n`;
    msg += `5. [FINISH] - 任务完成\n`;
    msg += `6. [ASK:问题内容] - 暂停操作，向用户提问，等待回复后继续\n`;
    msg += `7. [WAIT] - 暂停操作，等待用户后续指令\n`;
    msg += `[/WEB_CRUISE]`;

    const res = await fetch(`${BASE_URL}/api/${charId}/chat_v2`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Cookie': cookieString },
        body: JSON.stringify({ message: msg })
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    // full_reply 是后端新增字段，不斜线拆分，保留完整标签格式
    if (data.full_reply) return data.full_reply;
    // fallback: 自行拼接（但 URL 中的 // 会变成 /，备用）
    if (data.replies && data.replies.length > 0) {
        return data.replies.map(r => r.content).join(' / ');
    }
    return '';
}

async function notifyUser(charId, userId, content, cookieString) {
    try {
        await fetch(`${BASE_URL}/api/agent/notify_user`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Cookie': cookieString },
            body: JSON.stringify({ char_id: charId, user_id: userId, content })
        });
    } catch (e) {
        console.warn(`[Agent] 通知用户失败:`, e.message);
    }
}

// ==================== 网页操作 ====================

async function waitForPageSettled(page) {
    try {
        await page.waitForLoadState('domcontentloaded', { timeout: 5000 });
    } catch (e) {}
    try {
        await page.waitForLoadState('networkidle', { timeout: 3000 });
    } catch (e) {}
}

async function captureSnapshot(page) {
    for (let attempt = 1; attempt <= 3; attempt++) {
        try {
            await waitForPageSettled(page);
            return await page.evaluate(() => {
                const bodyText = document.body ? document.body.innerText : '';
                const visibleText = bodyText.replace(/\n{2,}/g, '\n').substring(0, 2000);
                const inputs = Array.from(document.querySelectorAll('input, textarea')).map(i => {
                    return `[输入框] ID:${i.id || '无'} Name:${i.name || '无'} Placeholder:${i.placeholder || '无'}`;
                }).join('\n');
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [onclick], input[type="submit"], input[type="button"]')).slice(0, 30).map(el => {
                    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().substring(0, 50);
                    const cls = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
                    const sel = el.id ? '#' + el.id : (el.name ? '[name="' + el.name + '"]' : cls ? '.' + cls.split(' ')[0] : '');
                    return `[按钮] 文字:"${text}" ${sel ? '选择器:' + sel : ''} 标签:${el.tagName.toLowerCase()}`;
                }).join('\n');
                return { text: visibleText, inputs, buttons, url: window.location.href };
            });
        } catch (err) {
            const isNavigationRace = /Execution context was destroyed|Cannot find context|Target page, context or browser has been closed/i.test(err.message);
            if (!isNavigationRace || attempt === 3) {
                console.warn(`[Snapshot Error] 截取页面失败: ${err.message}`);
                return {
                    text: '页面正在跳转或暂时无法读取，请根据当前网址继续判断下一步。',
                    inputs: '',
                    buttons: '',
                    url: page.url()
                };
            }
            await page.waitForTimeout(1000);
        }
    }
}

async function executeCommands(page, reply) {
    const commands = reply.match(/\[(CLICK|TYPE|GOTO|BACK):.+?\]/gi) || [];
    const backMatch = reply.match(/\[BACK\]/i);
    if (backMatch) commands.push('[BACK]');

    for (const cmd of commands) {
        try {
            if (cmd.startsWith('[CLICK:')) {
                let target = cmd.slice(7, -1);
                const hadPrefix = target.startsWith('ID:') || target.startsWith('Name:');
                if (target.startsWith('ID:')) {
                    target = '#' + target.slice(3);
                } else if (target.startsWith('Name:')) {
                    target = '[name="' + target.slice(5) + '"]';
                }
                console.log(`[Action] 点击: ${target}`);
                const textLoc = page.getByText(target, { exact: false }).first();
                if (await textLoc.count() > 0) {
                    await textLoc.click({ timeout: 3000 });
                } else if (!hadPrefix && cmd.slice(7, -1).indexOf('|') === -1) {
                    try {
                        await page.click('#' + cmd.slice(7, -1), { timeout: 3000 });
                    } catch {
                        await page.click(target, { timeout: 3000 });
                    }
                } else {
                    await page.click(target, { timeout: 3000 });
                }
            }
            else if (cmd.startsWith('[TYPE:')) {
                const inner = cmd.slice(6, -1);
                const pipeIdx = inner.indexOf('|');
                let selector = pipeIdx >= 0 ? inner.substring(0, pipeIdx) : inner;
                const text = pipeIdx >= 0 ? inner.substring(pipeIdx + 1) : '';
                const hadPrefix = selector.startsWith('ID:') || selector.startsWith('Name:');
                if (selector.startsWith('ID:')) {
                    selector = '#' + selector.slice(3);
                } else if (selector.startsWith('Name:')) {
                    selector = '[name="' + selector.slice(5) + '"]';
                }
                console.log(`[Action] 输入: "${text}" -> ${selector}`);
                const inputLoc = page.getByPlaceholder(selector, { exact: false }).first();
                if (await inputLoc.count() > 0) {
                    await inputLoc.fill(text);
                } else if (!hadPrefix) {
                    const rawSelector = pipeIdx >= 0 ? inner.substring(0, pipeIdx) : inner;
                    try {
                        await page.fill('#' + rawSelector, text);
                    } catch {
                        try {
                            await page.fill('[name="' + rawSelector + '"]', text);
                        } catch {
                            await page.fill(selector, text);
                        }
                    }
                } else {
                    await page.fill(selector, text);
                }
            }
            else if (cmd.startsWith('[GOTO:')) {
                let url = cmd.slice(6, -1);
                url = url.replace(/^https:(?!\/\/)/, 'https://');
                url = url.replace(/^http:(?!\/\/)/, 'http://');
                console.log(`[Action] 导航至: ${url}`);
                await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
                await waitForPageSettled(page);
            }
            else if (cmd === '[BACK]') {
                console.log(`[Action] 回退页面`);
                await page.goBack({ waitUntil: 'domcontentloaded', timeout: 15000 });
                await waitForPageSettled(page);
            }
        } catch (err) {
            console.warn(`[Action Error] 执行指令 ${cmd} 失败:`, err.message);
        }
    }
}

function hasWebCommands(text) {
    return /\[(CLICK|TYPE|GOTO):.+?\]/i.test(text) || /\[BACK\]/i.test(text) || /\[FINISH\]/i.test(text);
}

// ==================== 主循环 ====================

async function runUniversalAgent(targetUrl) {
    const browser = await chromium.launch({
        headless: true,
        slowMo: 50
    });
    const context = await browser.newContext({
        viewport: { width: 1280, height: 800 }
    });
    const page = await context.newPage();

    const targetCharId = process.argv[3] || 'reo';
    const currentUserId = process.argv[4] || 1;

    console.log(`[Universal Agent] 目标网址: ${targetUrl}`);
    console.log(`[Universal Agent] 角色: ${targetCharId}, 用户: ${currentUserId}`);

    // 生成身份认证 Cookie
    let cookieString = '';
    try {
        const pyScript = `import sys, os; root_dir = '${ROOT_DIR}'; sys.path.insert(0, root_dir); from app import app; from flask.sessions import SecureCookieSessionInterface; si = SecureCookieSessionInterface(); s = si.session_class(); s['user_id'] = ${currentUserId}; s['logged_in'] = True; val = si.get_signing_serializer(app).dumps(dict(s)); print(f'session={val}')`;
        const pyCmd = path.join(ROOT_DIR, 'venv/bin/python3');
        const cookieOutput = execSync(`${pyCmd} -c "${pyScript}"`).toString();
        cookieString = cookieOutput.split('\n').find(line => line.trim().startsWith('session=')).trim();
        console.log("-> Successfully generated session cookie!");
    } catch (e) {
        console.warn("[Agent] 警告：无法生成认证 Cookie。原因: " + e.message);
    }

    const charId = targetCharId;
    const charName = charId;

    // 初始导航
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded' });

    // 初始化状态文件
    updateState(currentUserId, {
        running: true,
        char_id: charId,
        status: 'active',
        question: ''
    });

    // 注：人设与系统规则由后端 /api/<char>/chat_v2 统一加载（build_system_prompt_v2），
    // 上下文由 chat_v2 从聊天 DB 自动组装，Agent 不再本地维护对话历史与人设。

    // 主循环
    let step = 1;
    let status = 'active';

    while (step <= MAX_STEPS) {
        await page.waitForTimeout(1500);

        // === 检查 STOP 信号 ===
        const input = consumeInput(currentUserId);
        if (input && input.command === 'stop') {
            console.log("[Agent] 收到用户 STOP 信号，正在退出...");
            status = 'stopped';
            cleanupState(currentUserId);
            break;
        }

        // === 等待用户回复状态 ===
        if (status === 'waiting_for_user') {
            if (input && input.command === 'reply') {
                console.log(`[Agent] 收到用户回复: "${input.message}"`);
                // 用户回复已由 /api/agent/reply 存入聊天 DB，chat_v2 会自动读取为上下文
                status = 'active';
                updateState(currentUserId, { status: 'active', question: '' });
                // 转为 active 后不 continue，落入下方 active 块：
                // 会重新截取当前页面快照，AI 结合用户回复(DB上下文)+新页面继续
            } else if (input && input.command === 'web_action') {
                console.log(`[Agent] 收到 web_action 指令: ${input.tags}`);
                if (input.tags && input.tags.length > 0) {
                    for (const tag of input.tags) {
                        await executeCommands(page, tag);
                    }
                }
                status = 'active';
                updateState(currentUserId, { status: 'active', question: '' });
                // 执行完网页操作后，截新快照继续
            } else {
                // 还在等待中，继续轮询
                await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
                continue;
            }
        }

        // === 活跃状态：截取页面快照，调AI ===
        if (status === 'active') {
            // 只有正常活跃时才截新快照（非刚收到用户回复的过渡轮次）
            const snapshot = await captureSnapshot(page);
            console.log(`\n--- 第 ${step} 步 | ${charName}正在看: ${snapshot.url} ---`);

            // 调用 AI（通过 chat_v2：快照作为 [WEB_CRUISE] 用户输入，
            // 触发带人设的回复，并让前端渲染"正在阅读"，回复自动存库显示）
            let reply = '';
            try {
                reply = await chatViaCharApi(snapshot, charId, currentUserId, cookieString);
            } catch (e) {
                console.error("[Agent] AI 调用失败:", e.message);
                break;
            }

            // 清理 HTML 标签
            reply = reply.replace(/<\/?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?\/?>/g, '');

            console.log(`${charName}回复: \n${reply}`);

            // 解析回复中的指令
            const askMatch = reply.match(/\[ASK:(.+?)\]/i);
            const waitMatch = reply.match(/\[WAIT\]/i);
            const finishMatch = reply.match(/\[FINISH\]/i);

            if (askMatch) {
                // AI 提问用户（回复已由 chat_v2 存库并在前端显示，无需再 notifyUser）
                const question = askMatch[1].trim();
                console.log(`[Agent] AI 提问: "${question}"`);
                status = 'waiting_for_user';
                updateState(currentUserId, {
                    status: 'waiting_for_user',
                    question: question
                });
                step++;
                continue;
            }

            if (waitMatch) {
                // AI 暂停（回复已由 chat_v2 存库并在前端显示，无需再 notifyUser）
                console.log(`[Agent] AI 请求暂停 [WAIT]`);
                status = 'waiting_for_user';
                updateState(currentUserId, {
                    status: 'waiting_for_user',
                    question: ''
                });
                step++;
                continue;
            }

            if (finishMatch) {
                console.log("[Agent] AI 指示操作完成。");
                cleanupState(currentUserId);
                break;
            }

            // 执行网页操作指令
            await executeCommands(page, reply);

            step++;
        }
    }

    console.log(`[Agent] 任务结束。`);
    try { cleanupState(currentUserId); } catch (e) {}
    await browser.close();
}

// 获取命令行参数
const urlArg = process.argv[2] || 'https://www.baidu.com';
runUniversalAgent(urlArg).catch(err => {
    console.error(`[Agent Fatal] ${err.stack || err.message}`);
    process.exit(1);
});
