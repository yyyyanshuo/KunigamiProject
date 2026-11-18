const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const context = {window: {SakiI18n: {t: value => value}}};
vm.createContext(context);
for (const name of ['image-tags.js', 'image-composer.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'static', name), 'utf8'), context);
}
const compose = context.window.SakiImageComposer.compose;
const tag = '[IMAGE](photo.jpg)(猫/狗和 f(x/y))';
const cases = [
  ['', tag],
  ['[图片]', tag],
  ['前[图片]后', `前 / ${tag} / 后`],
  ['前/[图片]/后', `前/ ${tag} /后`],
  ['前 / [图片] / 后', `前 / ${tag} / 后`],
  ['[图片]后', `${tag} / 后`],
  ['前[图片]', `前 / ${tag}`],
  ['前/后', `前/后 / ${tag}`],
  ['前\n[图片]\n后', `前 / ${tag} / 后`],
  ['前[IMAGE]后', `前 / ${tag} / 后`],
  ['前[画像]后', `前 / ${tag} / 后`],
  ['https://example.com/a/b[图片]后', `https://example.com/a/b / ${tag} / 后`],
  ['https://example.com/a/[图片]', `https://example.com/a/ / ${tag}`],
  ['[IMAGE](old.jpg)(说明中的[图片])/新[图片]', `[IMAGE](old.jpg)(说明中的[图片])/新 / ${tag}`],
];
for (const [caption, expected] of cases) assert.equal(compose(caption, tag), expected, caption);
assert.throws(() => compose('[图片]和[IMAGE]', tag), /只能指定一个位置/);
const parts = context.window.KunigamiImageTags.splitBubbles(compose('今天买了新衣服/给你看看[图片]怎么样？', tag));
assert.deepEqual(Array.from(parts), ['今天买了新衣服', '给你看看', tag, '怎么样？']);
const prepare = context.window.SakiImageComposer.prepareTranslation;
for (const source of ['今天/给你看看[图片]怎么样？', '前[IMAGE]后', '前[画像]后',
    '链接 https://example.com/a/b?x=1&y=2 [图片]结尾', '[IMAGE](old.jpg)(说明中的[图片]/f(x/y))/新[图片]',
    '文字 __SAKI_CAPTION_0__ / [图片] 后文']) {
  const prepared = prepare(source);
  assert.equal(prepared.restore(prepared.text), source);
  assert.equal(prepared.hasText, true);
  assert(!prepared.text.includes('https://example.com') && !prepared.text.includes('old.jpg'));
  if (prepared.tokens.length) {
    assert.throws(() => prepared.restore(prepared.text.replace(prepared.tokens[0], '')), /图片位置或分隔符/);
    assert.throws(() => prepared.restore(prepared.text + prepared.tokens[0]), /图片位置或分隔符/);
  }
  assert.throws(() => prepared.restore(prepared.text + '/多了一段'), /图片位置或分隔符/);
  assert.throws(() => prepared.restore(prepared.text + '[图片]'), /图片位置或分隔符/);
}
const protectedCaption = prepare('前/[图片]/后');
assert.throws(() => protectedCaption.restore(protectedCaption.tokens.join('')), /图片位置或分隔符/);
assert.throws(() => protectedCaption.restore(protectedCaption.text.replace(/__SAKI_CAPTION_[02]__/g,
  token => token.endsWith('0__') ? '__SAKI_CAPTION_2__' : '__SAKI_CAPTION_0__')), /图片位置或分隔符/);
for (const source of ['', '[图片]', '/[画像]/', 'https://example.com/a/b']) assert.equal(prepare(source).hasText, false);
assert.throws(() => prepare('前[图片]后[画像]'), /只能指定一个位置/);
const html = fs.readFileSync(path.join(root, 'templates/chat.html'), 'utf8');
for (const match of html.replace(/<!--[\s\S]*?-->/g, '').matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
  new vm.Script(match[1].replace(/\{\{[\s\S]*?\}\}/g, '1').replace(/\{%[\s\S]*?%\}/g, ''));
}
console.log('PASS: image positions, existing separators, URLs, nested descriptions, multilingual aliases and inline syntax');
