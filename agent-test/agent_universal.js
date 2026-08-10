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

async function chatViaCharApi(snapshot, charId, userId, cookieString, lastActionResult = null) {
    // 用 WEB_CRUISE 包裹页面快照，作为 user 消息发给 chat_v2
    // chat_v2 自带人设加载 + 存库，前端自动渲染"正在阅读"cruise-row
    let msg = `[WEB_CRUISE:${snapshot.url}]\n【当前页面文本】\n${snapshot.text}\n`;
    if (snapshot.inputs) msg += `\n【可用的输入框】\n${snapshot.inputs}\n`;
    if (snapshot.buttons) msg += `\n【可点击的按钮/链接】\n${snapshot.buttons}\n`;
    if (lastActionResult) {
        msg += `\n【上一轮浏览器操作结果】\n`;
        msg += `指令：${lastActionResult.command}\n`;
        msg += `状态：${lastActionResult.success ? '成功' : '失败'}\n`;
        msg += `说明：${lastActionResult.message}\n`;
    }
    msg += `\n请根据你的性格和当前页面内容，自然地和用户交谈，并在回复末尾附带网页操作指令:\n`;
    msg += `1. [CLICK_REF:元素编号] - 点击快照中的指定元素（优先使用）\n`;
    msg += `2. [CLICK:纯文字] - 按文字点击页面元素（仅在没有元素编号时使用）\n`;
    msg += `3. [TYPE:输入框描述|文本内容] - 在输入框输入内容\n`;
    msg += `4. [GOTO:URL] - 跳转到新网址\n`;
    msg += `5. [BACK] - 返回上一页\n`;
    msg += `6. [FINISH] - 任务完成\n`;
    msg += `7. [ASK:问题内容] - 暂停操作，向用户提问，等待回复后继续\n`;
    msg += `8. [WAIT] - 暂停操作，等待用户后续指令\n`;
    msg += `\n【重要操作规则】\n`;
    msg += `- 每轮只能输出一个网页操作指令。\n`;
    msg += `- 页面需要分步骤操作时，只执行当前第一步，等待新页面快照后再决定下一步。\n`;
    msg += `- enabled=false 的元素当前不可点击，不要对它输出点击指令。\n`;
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
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], [onclick], input[type="submit"], input[type="button"]')).slice(0, 30).map((el, index) => {
                    const ref = `btn-${index}`;
                    el.setAttribute('data-kunigami-agent-ref', ref);
                    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().substring(0, 50);
                    const enabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true';
                    const ariaPressed = el.getAttribute('aria-pressed');
                    const ariaSelected = el.getAttribute('aria-selected');
                    const hasCheckedState = typeof el.checked === 'boolean';
                    let selected = 'unknown';
                    if (ariaPressed !== null) selected = ariaPressed === 'true';
                    else if (ariaSelected !== null) selected = ariaSelected === 'true';
                    else if (hasCheckedState) selected = el.checked;
                    return `[按钮 ref="${ref}" enabled=${enabled} selected=${selected}] 文字:"${text}" 标签:${el.tagName.toLowerCase()} 建议指令:[CLICK_REF:${ref}]`;
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

function normalizeClickableText(text) {
    return String(text || '')
        .normalize('NFKC')
        .replace(/\s+/g, '')
        .replace(/[\uFE0F\u200D]/g, '')
        .trim();
}

function escapeAttributeValue(value) {
    return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

async function findClickableByText(page, target) {
    const candidates = page.locator(
        'button, a, [role="button"], [onclick], input[type="submit"], input[type="button"]'
    );
    const expected = normalizeClickableText(target);

    if (!expected) return null;

    const count = await candidates.count();
    for (let index = 0; index < count; index++) {
        const candidate = candidates.nth(index);
        if (!await candidate.isVisible().catch(() => false)) continue;

        const innerText = await candidate.innerText().catch(() => '');
        const value = await candidate.getAttribute('value').catch(() => '');
        const ariaLabel = await candidate.getAttribute('aria-label').catch(() => '');
        const actual = normalizeClickableText(innerText || value || ariaLabel);

        if (actual === expected || actual.includes(expected) || expected.includes(actual)) {
            return candidate;
        }
    }

    return null;
}

async function executeCommands(page, reply) {
    const commands = reply.match(/\[(?:CLICK_REF|CLICK|TYPE|GOTO):.+?\]|\[BACK\]/gi) || [];
    const cmd = commands[0];

    if (!cmd) return null;

    try {
        const clickRefMatch = cmd.match(/^\[CLICK_REF:(.+?)\]$/i);
        const clickMatch = cmd.match(/^\[CLICK:(.+?)\]$/i);
        const typeMatch = cmd.match(/^\[TYPE:(.+?)\]$/i);
        const gotoMatch = cmd.match(/^\[GOTO:(.+?)\]$/i);

        if (clickRefMatch) {
            const ref = clickRefMatch[1].trim();
            console.log(`[Action] 按编号点击: ${ref}`);
            const locator = page.locator(
                `[data-kunigami-agent-ref="${escapeAttributeValue(ref)}"]`
            ).first();

            if (await locator.count() === 0) {
                throw new Error(`元素编号已失效: ${ref}`);
            }
            if (!await locator.isVisible()) {
                throw new Error(`元素不可见: ${ref}`);
            }
            if (!await locator.isEnabled()) {
                throw new Error(`元素当前不可点击或已禁用: ${ref}`);
            }

            await locator.click({ timeout: 5000 });
        }
        else if (clickMatch) {
            const target = clickMatch[1].trim();
            console.log(`[Action] 按文字点击: ${target}`);

            let locator = null;
            if (target.startsWith('ID:')) {
                locator = page.locator(
                    `[id="${escapeAttributeValue(target.slice(3))}"]`
                ).first();
            } else if (target.startsWith('Name:')) {
                locator = page.locator(
                    `[name="${escapeAttributeValue(target.slice(5))}"]`
                ).first();
            } else if (target.startsWith('CSS:')) {
                locator = page.locator(target.slice(4)).first();
            } else {
                locator = await findClickableByText(page, target);
            }

            if (!locator || await locator.count() === 0) {
                throw new Error(`找不到可点击元素: ${target}`);
            }
            if (!await locator.isVisible()) {
                throw new Error(`元素不可见: ${target}`);
            }
            if (!await locator.isEnabled()) {
                throw new Error(`元素当前不可点击或已禁用: ${target}`);
            }

            await locator.click({ timeout: 5000 });
        }
        else if (typeMatch) {
            const inner = typeMatch[1];
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
        else if (gotoMatch) {
            let url = gotoMatch[1];
            url = url.replace(/^https:(?!\/\/)/, 'https://');
            url = url.replace(/^http:(?!\/\/)/, 'http://');
            console.log(`[Action] 导航至: ${url}`);
            await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
            await waitForPageSettled(page);
        }
        else if (/^\[BACK\]$/i.test(cmd)) {
            console.log(`[Action] 回退页面`);
            await page.goBack({ waitUntil: 'domcontentloaded', timeout: 15000 });
            await waitForPageSettled(page);
        }

        return {
            command: cmd,
            success: true,
            message: '操作执行成功'
        };
    } catch (err) {
        console.warn(`[Action Error] 执行指令 ${cmd} 失败:`, err.message);
        return {
            command: cmd,
            success: false,
            message: err.message
        };
    }
}

function hasWebCommands(text) {
    return /\[(CLICK_REF|CLICK|TYPE|GOTO):.+?\]/i.test(text) || /\[BACK\]/i.test(text) || /\[FINISH\]/i.test(text);
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
    let lastActionResult = null;

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
                    // 即使一次收到多个标签，本轮也只执行第一个网页动作。
                    lastActionResult = await executeCommands(page, input.tags.join('\n'));
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
                const actionResultForThisStep = lastActionResult;
                lastActionResult = null;
                reply = await chatViaCharApi(
                    snapshot,
                    charId,
                    currentUserId,
                    cookieString,
                    actionResultForThisStep
                );
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
            lastActionResult = await executeCommands(page, reply);

            step++;
        }
    }

    console.log(`[Agent] 任务结束。`);
    try { cleanupState(currentUserId); } catch (e) {}
    await browser.close();
}

module.exports = {
    captureSnapshot,
    chatViaCharApi,
    executeCommands,
    findClickableByText,
    hasWebCommands,
    normalizeClickableText,
    runUniversalAgent
};

if (require.main === module) {
    // 获取命令行参数
    const urlArg = process.argv[2] || 'https://www.baidu.com';
    runUniversalAgent(urlArg).catch(err => {
        console.error(`[Agent Fatal] ${err.stack || err.message}`);
        process.exit(1);
    });
}
