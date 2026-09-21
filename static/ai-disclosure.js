(function(global) {
  'use strict';

  var BRAND_LINE = '由 Sakura樱语🌸 AI 生成';
  var SITE_URL = 'https://kunigami-project-api.online';
  var COPY_FOOTER = '——\n' + BRAND_LINE + '\n' + SITE_URL;

  function appendCopyFooter(value) {
    var text = String(value || '').trim();
    if (!text) return COPY_FOOTER;
    if (text.endsWith(COPY_FOOTER)) return text;
    return text + '\n\n' + COPY_FOOTER;
  }

  global.KunigamiAiDisclosure = Object.freeze({
    brandLine: BRAND_LINE,
    siteUrl: SITE_URL,
    copyFooter: COPY_FOOTER,
    appendCopyFooter: appendCopyFooter
  });
})(window);
