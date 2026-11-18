const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'templates/moments.html'), 'utf8');
const catalog = JSON.parse(fs.readFileSync(path.join(root, 'static/locales/ui.json'), 'utf8'));
function section(start, end) {
    const index = source.indexOf(start);
    assert.ok(index >= 0, start);
    const last = source.indexOf(end, index);
    assert.ok(last > index, end);
    return source.slice(index, last);
}
const parser = section('async function parseMomentReplyResponse', '// 新增：回复弹窗动态打开方法');
const opener = section('window.openReplyModal = function', 'if (replyModalMask && replyCharacterSelect)');
const submitter = section("replyModalSubmit.addEventListener('click'", '// 用户亲自回复发送');

function setup(language = 'zh-CN') {
    const requests = [], alerts = [];
    const context = {
        window: {},
        SakiI18n: {t: key => catalog[key]?.[language] || key, fragment: text => text},
        replyModalMask: {dataset: {}, style: {}},
        replyCharacterSelect: {value: 'speaker', innerHTML: ''},
        replyModalSubmit: {innerHTML: 'Generate', disabled: false, addEventListener(event, callback) {this.click = callback;}},
        allMoments: [{char_id: 'author', timestamp: 'time', comments: [{comment_index: 0, commenter_id: 'target'}]}],
        escapeHtml: text => text,
        escapeAttr: text => text,
        encodeURIComponent,
        alert: text => alerts.push(text),
        closeReplyModal: () => {context.closed = true;},
        reloadAndScrollTo: value => {context.reloaded = value;},
        fetch: async (url, options) => {
            requests.push({url, options});
            return Response.json(options ? {status: 'success'} : {speaker: 'Speaker'});
        },
    };
    vm.createContext(context);
    vm.runInContext(parser + opener + submitter, context);
    return {context, requests, alerts};
}

test('numeric comment zero selects its author and sends the AI reply route', async () => {
    const {context, requests, alerts} = setup();
    context.window.openReplyModal('author', 'time', 0);
    assert.equal(context.replyModalMask.dataset.commentIndex, '0');
    assert.match(requests[0].url, /target_id=target$/);
    await new Promise(setImmediate);
    context.replyModalSubmit.click();
    assert.equal(context.replyModalSubmit.disabled, true);
    await new Promise(setImmediate);
    assert.equal(requests[1].url, '/api/moments/comment/ai_reply');
    assert.deepEqual(JSON.parse(requests[1].options.body), {
        char_id: 'author', timestamp: 'time', comment_index: '0', replying_char_id: 'speaker',
    });
    assert.equal(context.closed, true);
    assert.equal(context.reloaded.char_id, 'author');
    assert.equal(context.replyModalSubmit.disabled, false);
    assert.equal(context.replyModalSubmit.innerHTML, 'Generate');
    assert.equal(alerts.length, 0);
});

test('replying to a post still calls the post comment route', async () => {
    const {context, requests} = setup();
    context.window.openReplyModal('author', 'time');
    await new Promise(setImmediate);
    context.replyModalSubmit.click();
    await new Promise(setImmediate);
    assert.equal(requests[1].url, '/api/moments/post/ai_comment');
    assert.equal(JSON.parse(requests[1].options.body).commenter_id, 'speaker');
});

for (const status of [200, 404, 502, 504, 524]) {
    test(`HTML response ${status} produces a readable error and resets submit`, async () => {
        const {context, alerts} = setup();
        context.replyModalMask.dataset = {charId: 'author', timestamp: 'time', commentIndex: '0'};
        context.fetch = async () => new Response('<!doctype html><title>Gateway error</title>', {
            status, headers: {'Content-Type': 'text/html'},
        });
        context.replyModalSubmit.click();
        await new Promise(setImmediate);
        assert.equal(alerts.length, 1);
        assert.match(alerts[0], new RegExp(`HTTP ${status}`));
        assert.doesNotMatch(alerts[0], /SyntaxError|Unexpected token|doctype|Gateway error/);
        assert.equal(context.closed, undefined);
        assert.equal(context.reloaded, undefined);
        assert.equal(context.replyModalSubmit.disabled, false);
        assert.equal(context.replyModalSubmit.innerHTML, 'Generate');
    });
}

test('malformed JSON and invalid payload shapes never appear successful', async () => {
    const {context} = setup();
    for (const body of ['<html>oops', '', 'null', '[]', '"text"']) {
        await assert.rejects(context.parseMomentReplyResponse(new Response(body, {
            headers: {'Content-Type': 'application/json'},
        })), error => /HTTP 200/.test(error.message) && !/SyntaxError/.test(error.message));
    }
    await assert.rejects(context.parseMomentReplyResponse(Response.json({}, {status: 500})), /HTTP 500/);
    await assert.rejects(context.parseMomentReplyResponse(Response.json({error: 'Model failed'})), /Model failed/);
    await assert.rejects(context.parseMomentReplyResponse(Response.json({status: 'error', message: 'Busy'})), /Busy/);
});

test('login redirects and unauthorized responses ask the user to log in', async () => {
    const {context} = setup();
    const response = new Response('<html>login', {headers: {'Content-Type': 'text/html'}});
    Object.defineProperty(response, 'redirected', {value: true});
    await assert.rejects(context.parseMomentReplyResponse(response), /请先登录/);
    await assert.rejects(context.parseMomentReplyResponse(Response.json({}, {status: 401})), /请先登录/);
});

test('fallback messages use each supported UI language', async () => {
    for (const language of ['zh-CN', 'ja', 'en']) {
        const {context} = setup(language);
        await assert.rejects(context.parseMomentReplyResponse(new Response('timeout', {status: 504})), error =>
            error.message.includes(catalog['请求超时，请稍后重试。'][language]));
    }
});


test('blocked related characters are disabled and cannot be submitted manually', async () => {
    const {context, requests, alerts} = setup();
    context.allMoments[0].blocked_character_ids = ['speaker'];
    context.window.openReplyModal('author', 'time');
    await new Promise(setImmediate);
    assert.match(context.replyCharacterSelect.innerHTML, /value="speaker" disabled/);
    context.replyModalSubmit.click();
    await new Promise(setImmediate);
    assert.equal(requests.length, 1);
    assert.match(alerts[0], /不可见/);
});
