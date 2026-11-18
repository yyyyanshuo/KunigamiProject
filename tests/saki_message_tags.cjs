const assert = require('node:assert/strict');
global.window = global;
require('../static/image-tags.js');
require('../static/message-tags.js');
const samples = [
 ['[表情]official:happy.png', '[STICKER]official:happy.png'],
 ['[转账:100円|note]', '[TRANSFER:100円|note]'],
 ['[领取转账:100円]', '[TRANSFER_ACCEPT:100円]'],
 ['[退回转账:100円]', '[TRANSFER_RETURN:100円]'],
 ['[已领取转账:100円]', '[TRANSFER_ACCEPTED:100円]'],
 ['[已退回转账:100円]', '[TRANSFER_RETURNED:100円]'],
 ['[文件]a.txt', '[FILE]a.txt'], ['[音乐]song', '[MUSIC]song'],
 ['[リンク]site','[LINK]site'], ['[ビデオ通話]','[VIDEO_CALL]'],
 ['[未返信]','[UNANSWERED]'], ['[Read Receipt]','[READ_RECEIPT]'],
];
for(const [old,canonical] of samples) {
 assert.equal(SakiMessageTags.canonical(old),canonical);
 assert.equal(SakiMessageTags.canonical(canonical),canonical);
}
const old='[图片](a/b(1).png)(x(y/z) and [表情] as data)';
const canonical='[IMAGE](a/b(1).png)(x(y/z) and [表情] as data)';
assert.equal(SakiMessageTags.canonical(old),canonical);
assert.equal(SakiMessageTags.legacy(canonical),old);
assert.deepEqual(KunigamiImageTags.splitBubbles(old+' / '+canonical),[old,canonical]);
assert.equal(KunigamiImageTags.findAll(canonical)[0].path,'a/b(1).png');
assert.equal(SakiMessageTags.canonical('[constructor]'),'[constructor]');
console.log('PASS browser protocol aliases, canonical output, balanced images and opaque descriptions');
