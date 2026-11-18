/* Display-only readings for explicitly identified prose and chat content. */
(function () {
  'use strict';
  if (!window.SakiFuriganaEnabled) return;
  const selector = '.monologue:not([lang]), .opening, [data-model-output], [data-furigana-output], .forum-post-content, .forum-thread-title, #module-base .view-mode [data-role-prose], #module-relation .view-mode .mem-text, #module-short .time-capsule [data-role-prose], #module-medium .view-mode .mem-text, #module-long .view-mode .mem-text';
  const cache = new Map();
  const pending = new WeakSet();
  const skipped = 'script,style,textarea,input,button,select,code,pre,a,ruby,[contenteditable="true"]';
  function plain(node) {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('rt,rp').forEach(el => el.remove());
    return copy.textContent;
  }
  function render(text, result) {
    const fragment = document.createDocumentFragment();
    const regex = /<ruby>([^<>]*)<rt>([^<>]*)<\/rt><\/ruby>/g;
    let match, last = 0, reconstructed = '';
    const decode = value => { const t = document.createElement('textarea'); t.innerHTML = value; return t.value; };
    while ((match = regex.exec(result))) {
      const before = result.slice(last, match.index), base = decode(match[1]);
      fragment.append(document.createTextNode(before)); reconstructed += before + base;
      const ruby = document.createElement('ruby'), rt = document.createElement('rt');
      ruby.append(document.createTextNode(base)); rt.textContent = decode(match[2]); ruby.append(rt); fragment.append(ruby);
      last = regex.lastIndex;
    }
    fragment.append(document.createTextNode(result.slice(last))); reconstructed += result.slice(last);
    return reconstructed === text ? fragment : null;
  }
  async function decorate(node) {
    const text = node.nodeValue;
    if (!text || !/[\u3041-\u3096\u30a1-\u30fa]/.test(text) || !/[\u3400-\u9fff]/.test(text) || text.length > 30000 || pending.has(node)) return;
    pending.add(node);
    try {
      if (!cache.has(text)) {
        const request = fetch('/api/furigana', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})})
          .then(response => {if (!response.ok) throw new Error();return response.json();}).then(data=>data.result || text);
        cache.set(text, request);
        if (cache.size > 500) cache.delete(cache.keys().next().value);
      }
      const result = await cache.get(text);
      if (!node.isConnected || node.nodeValue !== text || result === text) return;
      const fragment = render(text, result);
      if (fragment) node.replaceWith(fragment);
    } catch (_) { cache.delete(text); } finally { pending.delete(node); }
  }
  function scan() {
    document.querySelectorAll(selector).forEach(root => {
      if (root.closest(skipped)) return;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {acceptNode:node => node.parentElement.closest(skipped) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
      const nodes=[]; while(walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach(decorate);
    });
  }
  let timer;
  new MutationObserver(() => { clearTimeout(timer); timer=setTimeout(scan,120); }).observe(document.documentElement,{childList:true,subtree:true,characterData:true});
  document.addEventListener('copy', event => {
    const selection=window.getSelection();
    if (!selection || !selection.rangeCount || !selection.toString()) return;
    const fragment=selection.getRangeAt(0).cloneContents();
    if (!fragment.querySelector('ruby')) return;
    fragment.querySelectorAll('rt,rp').forEach(el=>el.remove());
    event.clipboardData.setData('text/plain',fragment.textContent); event.preventDefault();
  });
  window.SakiModelDisplay = {plain, preview(editor) {
    if (!editor) return;
    let preview=editor.nextElementSibling;
    if (!preview || !preview.hasAttribute('data-model-output')) {
      preview=document.createElement('div');preview.setAttribute('data-model-output','');preview.style.cssText='white-space:pre-wrap;max-height:220px;overflow:auto;padding:10px;';editor.after(preview);
    }
    preview.textContent=editor.value;
    editor.addEventListener('input',()=>preview.remove(),{once:true});
  }};
  scan();
})();
