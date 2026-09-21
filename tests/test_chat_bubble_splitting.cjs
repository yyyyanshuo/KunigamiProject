const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../templates/chat.html'), 'utf8');
const section = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a);
  return source.slice(a, b);
};
const context = vm.createContext({window: {}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/image-tags.js'), 'utf8'), context);
vm.runInContext(section('  function splitForwardedChatlogSegments(text)', '  // 不使用任意 innerHTML'), context);
vm.runInContext(`function splitBubbles(rest) {
  ${section('    // 分段：按 "/" 分类气泡', '    const segments = cleanedParts;')}
  return cleanedParts;
}`, context);
for (const fn of ['splitBubbles', 'splitForwardedChatlogSegments']) {
  for (const [text, expected] of [
    ['前/ヽ(｡>﹏< <｡)ﾉ/後/終', ['前', 'ヽ(｡>﹏< <｡)ﾉ', '後', '終']],
    ['前/1 < 2/後', ['前', '1 < 2', '後']],
    ['前/<未完成/後', ['前', '<未完成', '後']],
    ['<ruby>今日<rt>きょう</rt></ruby>/後', ['<ruby>今日<rt>きょう</rt></ruby>', '後']],
    ['前/ https://example.com/a/b /後', ['前', 'https://example.com/a/b', '後']],
    [' /前//後/ ', ['前', '後']],
  ]) {
    assert.deepEqual(Array.from(context[fn](text)), expected, `${fn}: ${text}`);
  }
}
for (const text of ['[HTML]<div>a/b</div>[/HTML]', '[图片](/static/a.png)(猫/犬)']) {
  assert.deepEqual(Array.from(context.splitBubbles(`前/${text}/後`)), ['前', text, '後']);
}
// Parse every inline script after replacing template expressions.
for (const match of source.replace(/<!--[\s\S]*?-->/g, '').matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  new vm.Script(match[1].replace(/\{\{[\s\S]*?\}\}/g, '1').replace(/\{%[\s\S]*?%\}/g, ''));
}
console.log('PASS: bubble/forwarded splitting with kaomoji, angle brackets, ruby, URLs, HTML and images; inline JS syntax');
