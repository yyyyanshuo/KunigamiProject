const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/96980/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const root = path.join(__dirname, '..');
const script = fs.readFileSync(path.join(root, 'static/forum-backgrounds.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'templates/forum.html'), 'utf8')
    .replace(/\{%[\s\S]*?%\}/g, '')
    .replace(/\{\{ url_for\('static', filename='forum-backgrounds.js'\) \}\}/g, '/static/forum-backgrounds.js')
    .replace(/\{\{ ui_language \}\}/g, 'zh-CN')
    .replace(/\{\{[\s\S]*?\}\}/g, 'Saki');
const clone = value => JSON.parse(JSON.stringify(value));
const rin = {id: 'character:rin', kind: 'character', label: '凛', content: '凛的原设定：<img src=x onerror="window.injected=true">'};
const sae = {id: 'character:sae', kind: 'character', label: '冴', content: '冴的设定'};
let sources = [clone(rin), sae, {id: 'user', kind: 'user', label: '桐奈', content: '用户设定'},
    {id: 'saved:1', kind: 'saved', label: '校园 AU', content: '旧的可复用背景'}];
let forums = [
    {id: 41, title: '续写测试', topic: '校园生活', forum_type: 'novel_readers', created_at: '2026-10-07',
        supplementary_info: '【角色设定：凛】\n' + rin.content, background_config: {items: [rin], note: ''}, posts: []},
    {id: 42, title: '旧版论坛', topic: '过去的故事', forum_type: 'world_bystanders', created_at: '2026-10-06', supplementary_info: '旧版手写背景'},
];
const requests = [];
let failSources = false;
let failGenerate = false;

(async () => {
    const browser = await chromium.launch({
        executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
        headless: true, args: ['--disable-background-networking', '--disable-sync', '--no-first-run'],
    });
    try {
        const context = await browser.newContext({viewport: {width: 1280, height: 1000}, serviceWorkers: 'block'});
        await context.addInitScript(() => {
            window.SakiI18n = {t: text => text, fragment: text => text,
                ui: (parts, ...values) => parts.reduce((result, part, index) => result + part + (values[index] ?? ''), '')};
        });
        await context.route('**/*', async route => {
            const url = new URL(route.request().url());
            if (url.hostname !== 'forum.test') return route.abort();
            if (url.pathname === '/forum') return route.fulfill({contentType: 'text/html', body: html});
            if (url.pathname === '/static/forum-backgrounds.js') return route.fulfill({contentType: 'application/javascript', body: script});
            if (url.pathname === '/api/forums/background-sources') return failSources
                ? route.fulfill({status: 500, json: {error: 'Simulated failure'}})
                : route.fulfill({json: {sources}});
            if (url.pathname === '/api/forums/list') return route.fulfill({json: forums});
            if (url.pathname === '/api/forums/generate-stream') {
                if (failGenerate) return route.fulfill({status: 400, json: {error: '模拟生成失败'}});
                const data = route.request().postDataJSON();
                requests.push(data);
                const prior = forums.find(forum => forum.id === data.historical_forum_id);
                const config = data.background.mode === 'save'
                    ? {items: data.background.items, note: data.background.note}
                    : prior?.background_config || {items: [], note: ''};
                const result = {...prior, id: prior?.id || 50, title: '续写测试', topic: data.topic,
                    forum_type: data.forum_type, posts: [], background_config: config, supplementary_info: config.note};
                if (data.background.name) {
                    result.saved_background = {id: 'saved:2', kind: 'saved', label: data.background.name, content: '已保存的组合内容'};
                    sources.push(result.saved_background);
                }
                forums = [result, ...forums.filter(forum => forum.id !== result.id)];
                return route.fulfill({contentType: 'text/event-stream', body: 'data: ' + JSON.stringify({done: true, forum: result}) + '\n\n'});
            }
            return route.fulfill({body: '', contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/plain'});
        });
        const page = await context.newPage();
        const errors = [];
        const expectedDialogs = [];
        page.on('pageerror', error => errors.push(error.message));
        page.on('dialog', async dialog => {
            if (expectedDialogs[0] === dialog.message()) expectedDialogs.shift();
            else errors.push(dialog.message());
            await dialog.dismiss();
        });
        await page.goto('http://forum.test/forum');
        await page.waitForFunction(() => document.querySelectorAll('#historyForumSelect option').length === 3
            && document.querySelectorAll('#backgroundSources .background-card').length === 4);
        await page.selectOption('#historyForumSelect', '41');
        assert.equal(await page.locator('#supplementaryInput').inputValue(), '');
        assert.equal(await page.locator('#backgroundSelection input').count(), 1);

        const charGroup = page.locator('[data-background-key="group:character"]');
        await charGroup.locator(':scope > summary').click();
        await charGroup.getByRole('checkbox', {name: '冴', exact: true}).check();
        assert.equal(await charGroup.getAttribute('open'), '');
        await page.locator('[data-background-key="selected:character:rin"] > summary').click();
        assert.equal(await page.locator('#backgroundSelection img').count(), 0);
        assert.equal(await page.evaluate(() => window.injected), undefined);

        sources[0] = {...rin, content: '凛已修改的设定'};
        await page.locator('#backgroundReload').click();
        const update = page.getByRole('button', {name: '更新为当前设定'});
        await update.waitFor();
        assert.equal((await page.evaluate(() => ForumBackgrounds.capture())).items[0].content, rin.content);
        await update.click();
        assert.equal((await page.evaluate(() => ForumBackgrounds.capture())).items[0].content, sources[0].content);

        await page.selectOption('#backgroundMode', 'save');
        await page.locator('#backgroundName').fill('草稿组合');
        await page.evaluate(() => {
            chatSegments = [{contact_name: '凛', user_name: '桐奈', character_name: '糸師凛',
                messages: [{role: 'user', sender_name: '桐奈', content: '新的事件'}]}];
            renderSegments();
            saveFormState();
        });
        const draft = await page.evaluate(() => ForumBackgrounds.capture());
        await page.reload();
        await page.waitForFunction(() => document.getElementById('historyForumSelect').value === '41');
        assert.deepEqual(await page.evaluate(() => ForumBackgrounds.capture()), draft);
        assert((await page.locator('#segmentsList').textContent()).includes('桐奈 · 糸師凛'));
        assert.equal(await page.locator('#supplementaryInput').inputValue(), '');

        // A one-time change is sent once, then the stored snapshot is restored.
        await page.selectOption('#backgroundMode', 'once');
        await page.locator('#supplementaryInput').fill('临时补充');
        await page.locator('#generateBtn').click();
        await page.waitForFunction(() => !document.getElementById('generateBtn').disabled);
        assert.equal(requests.at(-1).background.mode, 'once');
        assert.equal(requests.at(-1).background.name, '');
        assert.equal(requests.at(-1).background.note, '临时补充');
        assert.equal(requests.at(-1).background.items.length, 2);
        assert.equal(requests.at(-1).chat_segments[0].user_name, '桐奈');
        assert.equal(requests.at(-1).chat_segments[0].character_name, '糸師凛');
        assert.equal(requests.at(-1).chat_segments[0].messages[0].sender_name, '桐奈');
        assert.deepEqual((await page.evaluate(() => ForumBackgrounds.capture())).items, [rin]);
        assert.equal(await page.locator('#supplementaryInput').inputValue(), '');

        await page.locator('[data-background-key="group:user"] > summary').click();
        await page.locator('#backgroundSources').getByRole('checkbox', {name: '桐奈', exact: true}).check();
        await page.locator('[data-background-key="group:saved"] > summary').click();
        await page.locator('#backgroundSources').getByRole('checkbox', {name: '校园 AU', exact: true}).check();
        await page.selectOption('#backgroundMode', 'save');
        await page.locator('#supplementaryInput').fill('关系尚未公开');
        await page.locator('#backgroundName').fill('校园与人物关系');
        await page.locator('#generateBtn').click();
        await page.waitForFunction(() => !document.getElementById('generateBtn').disabled);
        assert.equal(requests.at(-1).background.mode, 'save');
        assert.deepEqual(requests.at(-1).background.items.map(item => item.kind), ['character', 'user', 'saved']);
        assert.equal(requests.at(-1).background.name, '校园与人物关系');
        assert.equal(await page.locator('#backgroundName').inputValue(), '');
        assert.equal(await page.locator('#backgroundMode').inputValue(), 'once');
        assert.equal(await page.locator('#supplementaryInput').inputValue(), '关系尚未公开');
        assert.equal(await page.locator('[data-background-key="source:saved:2"]').count(), 1);

        failSources = true;
        const beforeFailure = await page.evaluate(() => ForumBackgrounds.capture());
        await page.locator('#backgroundReload').click();
        await page.waitForFunction(() => document.getElementById('backgroundStatus').textContent.includes('失败'));
        assert.deepEqual(await page.evaluate(() => ForumBackgrounds.capture()), beforeFailure);
        failSources = false;
        await page.locator('#backgroundReload').click();
        await page.waitForFunction(() => !document.getElementById('backgroundReload').disabled);
        assert.equal(await page.locator('#backgroundStatus').textContent(), '');

        await page.selectOption('#historyForumSelect', '42');
        assert.equal(await page.locator('#supplementaryInput').inputValue(), '旧版手写背景');
        assert.equal(await page.locator('#backgroundSelection input').count(), 0);
        await page.selectOption('#historyForumSelect', '41');
        assert.equal(await page.locator('#supplementaryInput').inputValue(), '关系尚未公开');
        assert.equal(await page.locator('#backgroundSelection input').count(), 3);
        await page.evaluate(() => backToEdit());
        await page.locator('#backgroundSelection').scrollIntoViewIfNeeded();
        if (process.argv[2]) await page.screenshot({path: path.join(process.argv[2], 'forum-backgrounds-desktop.png')});
        await page.setViewportSize({width: 390, height: 844});
        await page.locator('#backgroundSelection').scrollIntoViewIfNeeded();
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
        const panel = await page.locator('#settingsPanel').boundingBox();
        assert(panel.width <= 390);
        if (process.argv[2]) await page.screenshot({path: path.join(process.argv[2], 'forum-backgrounds-mobile.png')});
        failGenerate = true;
        const retryDraft = await page.evaluate(() => ForumBackgrounds.capture());
        expectedDialogs.push('模拟生成失败');
        await page.locator('#generateBtn').click();
        await page.waitForFunction(() => !document.getElementById('generateBtn').disabled);
        assert(await page.locator('#settingsPanel').isVisible());
        assert.deepEqual(await page.evaluate(() => ForumBackgrounds.capture()), retryDraft);
        await page.evaluate(() => ForumBackgrounds.restore({items: [], note: '', mode: 'save', name: '空背景'}));
        expectedDialogs.push('请先选择或填写背景，再命名保存。');
        await page.locator('#generateBtn').click();
        assert(await page.locator('#settingsPanel').isVisible());
        assert.deepEqual(expectedDialogs, []);
        assert.deepEqual(errors, []);
        console.log('PASS: multi-select, safe previews, explicit persona updates, picker drafts, one-time reset, saved combinations, reload recovery, legacy history, mobile layout');
    } finally {
        await browser.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
