/* Message text is data. Only the explicit ruby grammar produces inline markup. */
(function () {
  'use strict';
  const frames = new WeakMap();
  const t = value => window.SakiI18n ? window.SakiI18n.t(value) : value;
  const node = (tag, text, className) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (className) el.className = className;
    return el;
  };
  function decodeRuby(value) {
    const entities = {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#x27': "'", '#39': "'"};
    return value.replace(/&(amp|lt|gt|quot|apos|#x27|#39);/g, (_, name) => entities[name]);
  }
  function appendText(container, text) {
    String(text).split('\n').forEach((part, i) => {
      if (i) container.appendChild(node('br'));
      container.appendChild(document.createTextNode(part));
    });
  }
  function appendProse(container, value, options = {}) {
    const text = String(value ?? '');
    // This recognizes a small content protocol, not arbitrary HTML to sanitize.
    const tokens = /<ruby>([^<>]*)<rt>([^<>]*)<\/rt><\/ruby>|https?:\/\/[^\s<>"']+|\[(CLICK_REF|CLICK|TYPE|GOTO|ASK):([^\]\n]*)\]|\[(BACK|FINISH|WAIT|STOP)\]/g;
    let match, cursor = 0;
    while ((match = tokens.exec(text))) {
      appendText(container, text.slice(cursor, match.index));
      if (match[1] !== undefined) {
        const ruby = node('ruby', decodeRuby(match[1]));
        ruby.appendChild(node('rt', decodeRuby(match[2])));
        container.appendChild(ruby);
      } else if (/^https?:/.test(match[0])) {
        let url;
        try { url = new URL(match[0]); } catch (_) { /* Invalid URLs stay text. */ }
        if (!url || !['http:', 'https:'].includes(url.protocol)) {
          appendText(container, match[0]);
        } else {
          const link = node('a', match[0], 'chat-link');
          link.href = url.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          container.appendChild(link);
          if (options.onAgent) {
            const button = node('button', '🤖', 'agent-btn');
            button.type = 'button';
            button.title = t('让TA去看看');
            button.style.cssText = 'background:none;border:1px solid #ddd;border-radius:4px;padding:0 4px;cursor:pointer';
            button.addEventListener('click', () => options.onAgent(url.href));
            container.append(' ', button);
          }
        }
      } else {
        const kind = match[3] || match[5];
        const messages = {CLICK_REF: "点击了页面元素 '$1'", CLICK: "点击了 '$1'", TYPE: "在 '$1' 输入了 '$2'",
          GOTO: "正在前往 '$1'", ASK: '🤔 向你提问：$1', BACK: '执行了后退操作', FINISH: '操作已完成',
          WAIT: '⏸️ 操作已暂停', STOP: '🛑 Agent 已停止'};
        const args = (match[4] || '').split('|');
        const label = t(messages[kind]).replace(/\$(1|2)/g, (_, i) => args[Number(i) - 1] || '');
        container.appendChild(node('i', label, 'agent-' + kind.toLowerCase()));
      }
      cursor = tokens.lastIndex;
    }
    appendText(container, text.slice(cursor));
  }
  function renderProse(container, text, options) {
    container.replaceChildren();
    appendProse(container, text, options);
  }
  function createHtmlBlock(html) {
    // frame-src on the embedding document also blocks self-navigation of srcdoc.
    // A policy inside the child alone does not constrain that navigation.
    if (!document.querySelector('meta[data-saki-frame-policy]')) {
      const policy = node('meta');
      policy.httpEquiv = 'Content-Security-Policy';
      policy.content = "frame-src 'none'; object-src 'none'";
      policy.setAttribute('data-saki-frame-policy', '');
      document.head.appendChild(policy);
    }
    const nonce = Array.from(crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, '0')).join('');
    const wrapper = node('div', undefined, 'html-block-container');
    const iframe = node('iframe', undefined, 'html-block-iframe');
    iframe.title = 'HTML';
    // Only the fixed nonce-authorized resize script below can execute. User HTML,
    // inline event handlers, eval and external scripts have no script permission.
    iframe.setAttribute('sandbox', 'allow-scripts');
    iframe.setAttribute('referrerpolicy', 'no-referrer');
    iframe.setAttribute('allow', "camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'; payment 'none'");
    iframe.style.cssText = 'width:100%;height:150px;border:none;display:block;min-height:100px;max-height:1600px';
    const csp = "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; img-src data:; media-src data:; font-src data:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
    const resize = `(function(){'use strict';let pending=false;function send(){if(pending)return;pending=true;setTimeout(function(){pending=false;parent.postMessage({type:'sakiHtmlResize',token:'${nonce}',height:Math.min(1600,Math.max(100,document.body.scrollHeight))},'*');},100);}window.addEventListener('load',function(){new ResizeObserver(send).observe(document.body);send();});})();`;
    // Policy precedes every untrusted byte and cannot be relaxed by another meta.
    iframe.srcdoc = '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="' + csp + '"></head><body>' +
      String(html ?? '') + '<script nonce="' + nonce + '">' + resize + '<\/script></body></html>';
    frames.set(iframe, {token: nonce, last: -Infinity});
    wrapper.appendChild(iframe);
    return {wrapper, iframe};
  }
  function renderMessage(container, value, options) {
    container.replaceChildren();
    const text = String(value ?? '');
    const start = text.indexOf('[HTML]'), end = text.lastIndexOf('[/HTML]');
    if (start >= 0 && end > start) {
      appendProse(container, text.slice(0, start), options);
      container.appendChild(createHtmlBlock(text.slice(start + 6, end)).wrapper);
      appendProse(container, text.slice(end + 7), options);
    } else {
      appendProse(container, text, options);
    }
  }
  window.addEventListener('message', event => {
    const data = event.data;
    if (!data || data.type !== 'sakiHtmlResize' || event.origin !== 'null' ||
        !Number.isFinite(data.height) || data.height < 100 || data.height > 1600) return;
    for (const iframe of document.querySelectorAll('.html-block-iframe')) {
      const state = frames.get(iframe);
      if (!state || event.source !== iframe.contentWindow || data.token !== state.token) continue;
      const now = performance.now();
      if (now - state.last < 80) return;
      state.last = now;
      iframe.style.height = Math.ceil(data.height) + 'px';
      break;
    }
  });
  window.SakiSafeChat = Object.freeze({appendProse, renderProse, renderMessage, createHtmlBlock});
})();
