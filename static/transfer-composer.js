(function(global) {
  'use strict';
  const t = key => global.SakiI18n.t(key);
  function compose(caption, tag) {
    const text = String(caption || '').trim();
    return text ? text + (text.endsWith('/') && !/https?:\/\/\S+$/.test(text) ? ' ' : ' / ') + tag : tag;
  }
  // Same draft/translation contract as the image editor; no automatic send.
  function captionEditor(prefix, options) {
    const el = suffix => document.getElementById(prefix + suffix);
    const modal = el('Modal'), caption = el('Caption'), row = el('TranslationRow');
    const sourceInput = el('TranslationInput'), translate = el('TranslationButton'), status = el('Status');
    let state = null, busy = false;
    function controls() {
      modal.querySelectorAll('input,textarea,select,button').forEach(node => { node.disabled = busy; });
      translate.disabled = busy || !!state?.request;
      modal.querySelectorAll('[data-submit]').forEach(node => { node.disabled = busy || !!state?.request; });
    }
    function open() {
      state?.request?.abort();
      state = {draft:options.getDraft(), source:options.getDraftSource(), focus:document.activeElement};
      state.translated = state.source ? state.draft : null;
      caption.value = state.draft; sourceInput.value = ''; status.textContent = '';
      const settings = options.translationSettings(); row.hidden = !settings.enabled;
      sourceInput.placeholder = settings.placeholder;
      busy = false; controls(); modal.classList.add('show');
      modal.querySelector('[role="dialog"]').focus();
    }
    function close() {
      if (busy || !state) return;
      const prior = state; state = null; prior.request?.abort();
      modal.classList.remove('show'); prior.focus?.focus();
    }
    caption.oninput = () => { if (state) { state.source = null; state.translated = null; } };
    translate.onclick = async () => {
      if (!state || busy || state.request) return;
      const source = sourceInput.value.trim(); if (!source) return;
      const session = state, prior = caption.value;
      try {
        const prepared = global.SakiImageComposer.prepareTranslation(source);
        if (!prepared.hasText) return;
        session.request = new AbortController(); controls();
        const raw = await options.translate(prepared.text, {protectedTokens:prepared.tokens, signal:session.request.signal});
        if (state !== session) return;
        const result = prepared.restore(raw);
        if ((caption.value || caption.value !== prior) && !global.confirm(t('用翻译结果替换当前草稿？'))) return;
        caption.value = result; session.source = source; session.translated = result;
        if (sourceInput.value.trim() === source) sourceInput.value = '';
        status.textContent = '';
      } catch (error) { if (state === session) status.textContent = error.message; }
      finally { if (state === session) { session.request = null; controls(); } }
    };
    sourceInput.onkeydown = event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); translate.click(); }
    };
    modal.onclick = event => { if (event.target === modal) close(); };
    el('Cancel').onclick = close;
    modal.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key === 'Tab') {
        const nodes = Array.from(modal.querySelectorAll('button,input,textarea,select')).filter(node => !node.disabled && node.getClientRects().length);
        const index = nodes.indexOf(document.activeElement);
        if (nodes.length && (event.shiftKey ? index <= 0 : index < 0 || index === nodes.length - 1)) {
          event.preventDefault(); nodes[event.shiftKey ? nodes.length - 1 : 0].focus();
        }
      }
    });
    return {open, close, isOpen:()=>!!state, canSubmit:()=>!!state && !busy && !state.request,
      caption:()=>caption.value, source:()=>state?.translated === caption.value ? state.source : null,
      persisted:()=>{if(state) options.clearDraft(state.draft);},
      setBusy:value=>{busy=value;controls();}, notice:text=>{status.textContent=text;}};
  }
  function create(options) {
    const el = id => document.getElementById(id);
    const editor = captionEditor('transferSend', options);
    el('transferSendConfirm').onclick = async () => {
      if (!editor.canSubmit() || !options.canSend()) return;
      try {
        const amount = el('transferSendAmount').value.trim();
        const currency = el('transferSendCurrency').value.trim();
        const note = el('transferSendNote').value.trim();
        if (!/^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?$/.test(amount) || Number(amount) <= 0) throw new Error(t('请输入有效的转账金额（最多两位小数）'));
        if (!/^[\p{L}\p{Sc}]{1,12}$/u.test(currency)) throw new Error(t('请输入币种代码或货币符号'));
        if (Array.from(note).length > 80 || /[\[\]|/\r\n]/.test(note)) throw new Error(t('转账备注最多80字，不能包含标签或分隔符'));
        const tag = `[TRANSFER:${amount}${currency}${note ? '|'+note : ''}]`;
        const message = compose(editor.caption(), tag), source = editor.source();
        editor.setBusy(true); options.lock(true);
        await options.send(message, () => {
          editor.persisted(); editor.setBusy(false); editor.close();
        }, source);
      } catch (error) { editor.notice(error.message || t('发送失败，请重试')); }
      finally { editor.setBusy(false); options.lock(false); }
    };
    return {open:()=>{
      if (!options.canSend() || editor.isOpen()) return;
      el('transferSendAmount').value = ''; el('transferSendCurrency').value = 'CNY'; el('transferSendNote').value = '';
      editor.open();
    }, isOpen:editor.isOpen};
  }
  global.SakiTransferComposer = Object.freeze({compose, captionEditor, create});
})(window);
