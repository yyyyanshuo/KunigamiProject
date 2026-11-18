const { messages, buildPrompt } = require('./agent_prompts');
const path = require('path');
const fs = require('fs');

/**
 * Universal Agent for KunigamiProject
 * 使角色的 AI 能够像真人一样"操纵"浏览器：点击、输入、滚动、导航。
 * 支持 [ASK] 提问暂停、[WAIT] 自由对话暂停、用户 [STOP] 停止。
 */

// ==================== 配置 ====================
const BASE_URL = process.env.SAKI_AGENT_API_URL || 'http://127.0.0.1:8010';
const ROOT_DIR = process.env.SAKI_DATA_ROOT || '/var/lib/sakiglobal';
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
    const temporary = `${filePath}.${process.pid}.tmp`;
    try {
        fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
        fs.writeFileSync(temporary, JSON.stringify(data), { encoding: 'utf-8', mode: 0o600 });
        fs.renameSync(temporary, filePath);
    } finally {
        try { fs.unlinkSync(temporary); } catch (_) {}
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

async function chatViaCharApi(snapshot, charId, userId, cookieString, lastActionResult = null, uiLanguage = 'zh-CN', outputLanguage = uiLanguage) {
    const msg = buildPrompt(snapshot, lastActionResult, uiLanguage, outputLanguage);
    const res = await fetch(`${BASE_URL}/api/${charId}/chat_v2`, {
        method: 'POST',
        signal: AbortSignal.timeout(240000),
        redirect: 'error',
        headers: { 'Content-Type': 'application/json', 'Cookie': cookieString },
        body: JSON.stringify({ message: msg })
    });
    if (!res.ok) throw new Error('Chat request failed');
    const data = await res.json();
    if (data.error) throw new Error('Chat request failed');
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
            signal: AbortSignal.timeout(10000),
            redirect: 'error',
            headers: { 'Content-Type': 'application/json', 'Cookie': cookieString },
            body: JSON.stringify({ char_id: charId, user_id: userId, content })
        });
    } catch (e) {
        console.warn('[Agent] Notification failed');
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

async function captureSnapshot(page, uiLanguage = 'zh-CN') {
    const t = messages(uiLanguage);
    for (let attempt = 1; attempt <= 3; attempt++) {
        try {
            await waitForPageSettled(page);
            return await page.evaluate((t) => {
                const bodyText = document.body ? document.body.innerText : '';
                const visibleText = bodyText.replace(/\n{2,}/g, '\n').substring(0, 2000);
                const inputs = Array.from(document.querySelectorAll('input, textarea')).map(i => {
                    return `[${t.input}] ID:${i.id || t.none} Name:${i.name || t.none} Placeholder:${i.placeholder || t.none}`;
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
                    return `[${t.button} ref="${ref}" enabled=${enabled} selected=${selected}] ${t.text}:"${text}" ${t.tag}:${el.tagName.toLowerCase()} ${t.suggested}:[CLICK_REF:${ref}]`;
                }).join('\n');
                return { text: visibleText, inputs, buttons, url: window.location.href };
            }, t);
        } catch (err) {
            const isNavigationRace = /Execution context was destroyed|Cannot find context|Target page, context or browser has been closed/i.test(err.message);
            if (!isNavigationRace || attempt === 3) {
                console.warn('[Agent] Snapshot unavailable');
                return {
                    text: t.unavailable,
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

        if (actual && (actual === expected || actual.includes(expected) || expected.includes(actual))) {
            return candidate;
        }
    }

    return null;
}

async function executeCommands(page, reply, uiLanguage = 'zh-CN') {
    const t = messages(uiLanguage);
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

            const locator = page.locator(
                `[data-kunigami-agent-ref="${escapeAttributeValue(ref)}"]`
            ).first();

            if (await locator.count() === 0) {
                throw Object.assign(new Error(t.invalidRef), { localized: true });
            }
            if (!await locator.isVisible()) {
                throw Object.assign(new Error(t.invisible), { localized: true });
            }
            if (!await locator.isEnabled()) {
                throw Object.assign(new Error(t.disabled), { localized: true });
            }

            await locator.click({ timeout: 5000 });
        }
        else if (clickMatch) {
            const target = clickMatch[1].trim();


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
                throw Object.assign(new Error(t.missing), { localized: true });
            }
            if (!await locator.isVisible()) {
                throw Object.assign(new Error(t.invisible), { localized: true });
            }
            if (!await locator.isEnabled()) {
                throw Object.assign(new Error(t.disabled), { localized: true });
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

            validateTarget(url, t);
            await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
            await waitForPageSettled(page);
        }
        else if (/^\[BACK\]$/i.test(cmd)) {

            await page.goBack({ waitUntil: 'domcontentloaded', timeout: 15000 });
            await waitForPageSettled(page);
        }

        return {
            command: cmd,
            success: true,
            message: t.actionOK
        };
    } catch (err) {
        console.warn('[Agent] Browser action failed');
        return {
            command: cmd,
            success: false,
            message: err.localized ? err.message : t.actionFailed
        };
    }
}

function hasWebCommands(text) {
    return /\[(CLICK_REF|CLICK|TYPE|GOTO):.+?\]/i.test(text) || /\[BACK\]/i.test(text) || /\[FINISH\]/i.test(text);
}

// Browser cookies and local files are never shared with the application session.
function validateTarget(url, t = messages('ja')) {
    let parsed;
    try { parsed = new URL(url); } catch (_) { throw new Error(t.invalidURL); }
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
        (parsed.port && !['80', '443'].includes(parsed.port))) throw new Error(t.invalidURL);
}

async function launchBrowser(proxy) {
    if (!/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(proxy || '')) throw new Error('Public-web proxy required');
    const { chromium } = require('playwright');
    return chromium.launch({
        headless: true,
        proxy: { server: proxy, bypass: '<-loopback>' },
        args: ['--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
    });
}

async function runUniversalAgent(job) {
    const { user_id: uid, char_id: charId, url, cookie, ui_language: lang, output_language: output } = job;
    if (!Number.isSafeInteger(uid) || uid <= 0 || !/^[A-Za-z0-9_]+$/.test(charId) || !cookie) throw new Error('Invalid job');
    if (!/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(BASE_URL)) throw new Error('Invalid internal API');
    const t = messages(lang);
    validateTarget(url, t);
    let browser;
    let done = false;
    try {
        browser = await launchBrowser(job.proxy);
        const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, acceptDownloads: false, serviceWorkers: 'block' });
        await context.route('**/*', async route => {
            try { validateTarget(route.request().url(), t); } catch (_) { await route.abort(); return; }
            await route.continue();
        });
        const page = await context.newPage();
        context.on('page', popup => { if (popup !== page) popup.close().catch(() => {}); });
        page.setDefaultTimeout(15000);
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        updateState(uid, { running: true, char_id: charId, status: 'active', question: '' });
        let waiting = false;
        let previous = null;
        let step = 0;
        while (step < MAX_STEPS) {
            await page.waitForTimeout(1500);
            const input = consumeInput(uid);
            if (input?.command === 'stop') { done = true; break; }
            if (waiting) {
                if (input?.command === 'reply') {
                    waiting = false;
                } else if (input?.command === 'web_action' && Array.isArray(input.tags)) {
                    previous = await executeCommands(page, input.tags.join('\n'), lang);
                    waiting = false;
                } else {
                    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
                    continue;
                }
                updateState(uid, { status: 'active', question: '' });
            }
            const snapshot = await captureSnapshot(page, lang);
            let reply = await chatViaCharApi(snapshot, charId, uid, cookie, previous, lang, output);
            reply = reply.replace(/<\/?[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?\/?>/g, '');
            step++;
            console.log(`[Agent] Step ${step}`);
            const ask = reply.match(/\[ASK:([\s\S]+?)\]/i);
            if (ask || /\[WAIT\]/i.test(reply) || !hasWebCommands(reply)) {
                waiting = true;
                updateState(uid, { status: 'waiting_for_user', question: ask ? ask[1].trim() : t.waiting });
                previous = null;
            } else if (/\[FINISH\]/i.test(reply)) {
                done = true;
                break;
            } else {
                previous = await executeCommands(page, reply, lang);
            }
        }
        if (!done) await notifyUser(charId, uid, t.limit, cookie);
    } catch (_) {
        await notifyUser(charId, uid, t.failed, cookie);
        throw new Error('Browsing task failed');
    } finally {
        if (browser) await browser.close().catch(() => {});
        cleanupState(uid);
    }
}

module.exports = { captureSnapshot, chatViaCharApi, executeCommands, findClickableByText,
    hasWebCommands, normalizeClickableText, runUniversalAgent, validateTarget, launchBrowser };

if (require.main === module) {
    (async () => {
        const chunks = [];
        for await (const chunk of process.stdin) chunks.push(chunk);
        const job = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (process.argv.includes('--check')) {
            const browser = await launchBrowser(job.proxy);
            try {
                const page = await browser.newPage();
                await page.goto('about:blank');
                // This must fail through the proxy, even though the local web server exists.
                let blocked = false;
                try {
                    const response = await page.goto('http://127.0.0.1:8010/login', { timeout: 10000 });
                    blocked = !!response && response.status() === 403;
                } catch (_) { blocked = true; }
                if (!blocked) throw new Error('Private network is reachable');
                console.log('PASS: Chromium launched; private network request blocked');
            } finally { await browser.close(); }
        } else { await runUniversalAgent(job); }
    })().catch(() => {
        console.error('[Agent] Task or browser check failed');
        process.exitCode = 1;
    });
}
