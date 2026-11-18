const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../templates/forum.html'), 'utf8');
const script = template.match(/<script>\s*\/\/ ===== State =====([\s\S]*?)<\/script>/)[1];
const forums = [
    {id: 1, title: '第一帖', topic: '排练安排', forum_type: 'custom', custom_type_description: '校园音乐社', supplementary_info: '只有社团成员知道日期'},
    {id: 2, title: '第二帖', topic: '比赛安排', forum_type: 'world_bystanders', supplementary_info: '尚未公开参赛名单'},
];

function createPage(draft) {
    const elements = new Map();
    function element(id) {
        if (!elements.has(id)) elements.set(id, {value: '', style: {}, innerHTML: ''});
        return elements.get(id);
    }
    let selected = '';
    const select = {
        options: [{value: ''}],
        appendChild(option) { this.options.push(option); },
        insertBefore(option) { this.options.push(option); },
        get value() { return selected; },
        set value(value) {
            selected = this.options.some(option => String(option.value) === String(value)) ? String(value) : '';
        },
    };
    elements.set('historyForumSelect', select);
    element('forumTypeSelect').value = 'novel_readers';
    const storage = new Map(draft ? [['forumFormState', JSON.stringify(draft)]] : []);
    let finishList;
    const response = new Promise(resolve => { finishList = () => resolve({ok: true, json: async () => forums}); });
    const context = vm.createContext({
        document: {getElementById: element, createElement: () => ({value: '', textContent: ''})},
        sessionStorage: {getItem: key => storage.get(key) ?? null, removeItem: key => storage.delete(key), setItem: (key, value) => storage.set(key, value)},
        SakiI18n: {t: text => text, fragment: text => text, ui: (parts, ...values) => parts.reduce((text, part, i) => text + part + (values[i] ?? ''), '')},
        fetch: url => { assert.equal(url, '/api/forums/list'); return response; },
    });
    vm.runInContext(script, context);
    return {element, select, storage, context, finishList};
}

(async () => {
    const page = createPage();
    page.finishList();
    await new Promise(resolve => setImmediate(resolve));
    page.select.value = '1';
    page.context.onHistoryForumSelected();
    assert.equal(page.element('topicInput').value, '排练安排');
    assert.equal(page.element('forumTypeSelect').value, 'custom');
    assert.equal(page.element('customTypeInput').value, '校园音乐社');
    assert.equal(page.element('supplementaryInput').value, '只有社团成员知道日期');
    page.select.value = '2';
    page.context.onHistoryForumSelected();
    assert.equal(page.element('supplementaryInput').value, '尚未公开参赛名单');
    assert.equal(page.element('customTypeInput').value, '');

    page.context.refreshHistorySelect({...forums[1], supplementary_info: '更新后的比赛背景'});
    page.select.value = '1';
    page.context.onHistoryForumSelected();
    page.select.value = '2';
    page.context.onHistoryForumSelected();
    assert.equal(page.element('supplementaryInput').value, '更新后的比赛背景');

    page.element('supplementaryInput').value += '\n尚未保存的补充';
    page.context.saveFormState();
    const savedDraft = JSON.parse(page.storage.get('forumFormState'));
    const restored = createPage(savedDraft);
    assert.equal(restored.select.value, '');
    restored.finishList();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(restored.select.value, '2');
    assert.equal(restored.element('supplementaryInput').value, savedDraft.supplementary);

    const emptyDraft = createPage({...savedDraft, supplementary: ''});
    emptyDraft.finishList();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(emptyDraft.select.value, '2');
    assert.equal(emptyDraft.element('supplementaryInput').value, '尚未公开参赛名单');
    console.log('PASS: history autofill, switching, refreshed background, and picker draft restoration');
})().catch(error => { console.error(error); process.exitCode = 1; });
