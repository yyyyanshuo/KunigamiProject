(function(global) {
  'use strict';

  var PREFIX = '[图片]';

  function parseParenthesized(text, start) {
    if (start >= text.length || text[start] !== '(') return null;
    var depth = 1;
    var valueStart = start + 1;
    for (var i = valueStart; i < text.length; i++) {
      if (text[i] === '(') depth += 1;
      else if (text[i] === ')') {
        depth -= 1;
        if (depth === 0) return { value: text.slice(valueStart, i), end: i + 1 };
      }
    }
    return null;
  }

  function parseAt(text, start) {
    text = String(text || '');
    start = Number(start || 0);
    if (text.slice(start, start + PREFIX.length) !== PREFIX) return null;
    var pathGroup = parseParenthesized(text, start + PREFIX.length);
    if (!pathGroup) return null;
    var descriptionGroup = parseParenthesized(text, pathGroup.end);
    if (!descriptionGroup) return null;
    return {
      start: start,
      end: descriptionGroup.end,
      path: pathGroup.value,
      description: descriptionGroup.value,
      raw: text.slice(start, descriptionGroup.end)
    };
  }

  function findAll(text) {
    text = String(text || '');
    var result = [];
    var cursor = 0;
    while (cursor < text.length) {
      var start = text.indexOf(PREFIX, cursor);
      if (start < 0) break;
      var tag = parseAt(text, start);
      if (tag) {
        result.push(tag);
        cursor = tag.end;
      } else {
        cursor = start + PREFIX.length;
      }
    }
    return result;
  }

  function splitBubbles(text) {
    text = String(text || '');
    var parts = [];
    var current = '';
    var cursor = 0;
    while (cursor < text.length) {
      var tag = parseAt(text, cursor);
      if (tag) {
        current += tag.raw;
        cursor = tag.end;
        continue;
      }
      if (text[cursor] === '/') {
        if (current.trim()) parts.push(current.trim());
        current = '';
      } else {
        current += text[cursor];
      }
      cursor += 1;
    }
    if (current.trim()) parts.push(current.trim());
    return parts;
  }

  function strip(text) {
    text = String(text || '');
    var tags = findAll(text);
    if (!tags.length) return text;
    var chunks = [];
    var cursor = 0;
    tags.forEach(function(tag) {
      chunks.push(text.slice(cursor, tag.start));
      cursor = tag.end;
    });
    chunks.push(text.slice(cursor));
    return chunks.join('');
  }

  function protect(text, placeholderPrefix) {
    text = String(text || '');
    placeholderPrefix = placeholderPrefix || '__IMG_';
    var tags = findAll(text);
    var chunks = [];
    var cursor = 0;
    tags.forEach(function(tag, index) {
      chunks.push(text.slice(cursor, tag.start));
      chunks.push(placeholderPrefix + index + '__');
      cursor = tag.end;
    });
    chunks.push(text.slice(cursor));
    return { text: chunks.join(''), tags: tags };
  }

  global.KunigamiImageTags = {
    parseAt: parseAt,
    findAll: findAll,
    splitBubbles: splitBubbles,
    strip: strip,
    protect: protect
  };
})(window);
