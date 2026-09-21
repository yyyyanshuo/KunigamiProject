const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../templates/chat.html'), 'utf8');
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const element = () => ({dataset: {}, children: [], classList: {add() {}}, appendChild(child) {this.children.push(child);}, prepend(child) {this.children.unshift(child);}});
const context = vm.createContext({document: {createElement: element}, chatContainer: element(), parseChatTimestamp: text => new Date(text), bindCompactMessageActions() {}});
vm.runInContext(section('  function stripRubyTags(text)', '  async function openEditModal'), context);
vm.runInContext(section('  function isSystemPromptMessage(text)', '  function legacyGenerationErrorMessage'), context);
vm.runInContext(section('  function renderSystemPromptGroup(', '  function renderMessageGroup('), context);
const expected = '（系统提示：今日/明日）';
for (const text of [expected, '（系统提示：<ruby>今日<rt>きょう</rt></ruby>/明日）', '（<ruby>系统<rt>けいとう</rt></ruby>提示：今日/明日）']) {
  assert.equal(context.isSystemPromptMessage(text), true);
  for (const role of ['user', 'assistant']) {
    const group = context.renderSystemPromptGroup({id: 1, sourceText: text, role, timestamp: '2026-09-09'}, 'append');
    assert.equal(group.dataset.originalContent, expected);
    const stack = group.children[0].children[0];
    assert.equal(stack.children.length, 1);
    assert.equal(stack.children[0].children[0].textContent, expected);
  }
}
assert.equal(context.isSystemPromptMessage('普通消息：<ruby>今日<rt>きょう</rt></ruby>'), false);
console.log('PASS: system notice recognition, plain rendering and editing data, existing ruby annotations, user/assistant roles');
