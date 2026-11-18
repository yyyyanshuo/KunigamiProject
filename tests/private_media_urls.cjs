const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {
  window: { GLOBAL_COS_BASE_URL: '/api/media/cos', GLOBAL_USER_PREFIX: 'users/1', addEventListener() {} },
  document: { readyState: 'loading', addEventListener() {} },
  localStorage: { getItem() { return null; } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/theme-loader.js', 'utf8'), context);
const get = context.window.getCosUrl;
assert.equal(get('/api/media/cos/users/1/avatar.png'), '/api/media/cos/users/1/avatar.png');
assert.equal(get('/api/media/cos/users/1/characters/hero/avatar.png'), '/api/media/cos/users/1/characters/hero/avatar.png');
assert.match(get('users/1/characters/hero/avatar.png'), /^\/api\/media\/cos\/users\/1\/characters\/hero\/avatar.png\?t=/);
assert.match(get('characters/hero/avatar.png'), /^\/api\/media\/cos\/users\/1\/characters\/hero\/avatar.png\?t=/);
assert.equal(get('https://external.invalid/a.png'), 'https://external.invalid/a.png');
assert.equal(get('/char_assets/hero/avatar.png'), '/char_assets/hero/avatar.png');
vm.runInContext(fs.readFileSync('static/image-tags.js', 'utf8'), context);
const template = fs.readFileSync('templates/moments.html', 'utf8');
const start = template.indexOf('function renderGridImages(mObj)');
const end = template.indexOf('return html;', start) + 'return html;'.length;
assert.ok(start >= 0 && end > start);
context.escapeAttr = value => value;
vm.runInContext(template.slice(start, end) + '\n}', context);
const html = context.renderGridImages({content:'[图片](/api/media/cos/users/1/moments/202609/a.png)(photo)'});
assert.ok(html.includes('src="/api/media/cos/users/1/moments/202609/a.png"'));
assert.ok(!html.includes('/moments//api/'));
console.log('PASS：主题、角色头像和朋友圈媒体 URL 检查');
