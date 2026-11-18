const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../templates/chat.html'), 'utf8');
const start = source.indexOf('    // --- 【论坛体选择器】');
const end = source.indexOf('    // --- 【新增】多选生成 & 范围选择功能 ---', start);
assert(start >= 0 && end > start);
const picker = source.slice(start, end);

function message(sender, content, index, extra = {}) {
    return {
        dataset: {sender, timestamp: '2026-10-07T12:00:00.000Z', ...extra}, content, index,
        classList: {contains: name => sender === 'user' && name === 'user'},
        querySelector: () => ({textContent: '20:00'}),
        compareDocumentPosition(other) { return this.index < other.index ? 4 : 2; },
    };
}

async function select({group = false, messages}) {
    const storage = new Map();
    const context = vm.createContext({
        window: {location: {pathname: group ? '/chat/group/club' : '/chat/rin', search: '?forumPicker=1&forumName=' + encodeURIComponent('备注 100%'), href: ''}},
        document: {body: {classList: {add() {}}}, querySelectorAll: () => [...messages].reverse()},
        URLSearchParams, Node: {DOCUMENT_POSITION_FOLLOWING: 4}, setTimeout() {},
        sessionStorage: {setItem: (key, value) => storage.set(key, value)},
        SakiI18n: {t: value => value}, alert: message => { throw new Error(message); },
        isGroupMode: group, currentId: group ? 'club' : 'rin', charId: group ? 'club' : 'rin',
        currentUserName: '桐奈', currentConfig: group ? {name: '足球部'} : {name: '糸師凛', remark: '小凛'},
        charConfig: {name: 'Assistant'},
        groupMembers: {rin: {name: '糸師凛', remark: '小凛'}, sae: {name: '糸師冴', remark: '哥哥'}},
        contactsCache: {former: {name: '洁世一', remark: '前队友'}},
        preloadSelectedCallRecords: async () => true,
        selectedMessageResolvedContent: group => group.content,
    });
    vm.runInContext(picker, context);
    await context.forumPickerConfirm();
    assert.equal(context.window.location.href, '/forum');
    const result = JSON.parse(storage.get('forumPickerSegment'));
    assert.equal(result.contact_name, '备注 100%');
    assert.equal(result.user_name, '桐奈');
    assert(result.messages.every(message => message.timestamp === '2026-10-07T12:00:00.000Z'));
    return result;
}

(async () => {
    const single = await select({messages: [message('user', '你好', 0), message('assistant', '晚上好', 1)]});
    assert.equal(single.character_name, '糸師凛');
    assert.equal(single.contact_type, 'chat');
    assert.deepEqual(single.messages.map(m => [m.role, m.sender_name, m.content]), [
        ['user', '桐奈', '你好'], ['assistant', '糸師凛', '晚上好'],
    ]);
    // Loading only one side still retains both participants' names.
    assert.equal((await select({messages: [message('user', '晚安', 0)]})).character_name, '糸師凛');
    const group = await select({group: true, messages: [
        message('user', '大家好', 0), message('rin', '你好', 1), message('sae', '嗯', 2),
        message('former', '已离群的发言', 3), message('unknown_id', '没有姓名资料', 4),
        message('system', '场景提示', 5),
    ]});
    assert.equal(group.character_name, '');
    assert.equal(group.contact_type, 'group');
    assert.deepEqual(group.messages.map(m => [m.role, m.sender_name]), [
        ['user', '桐奈'], ['rin', '糸師凛'], ['sae', '糸師冴'],
        ['former', '洁世一'], ['unknown_id', 'unknown_id'], ['system', ''],
    ]);
    console.log('PASS: picker snapshots actual user/character names, preserves group speakers, message order and full timestamps, and handles names containing %');
})().catch(error => { console.error(error); process.exitCode = 1; });
