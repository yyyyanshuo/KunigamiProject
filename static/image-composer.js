(function(global) {
  'use strict';
  const t = text => global.SakiI18n.t(text);
  const PREVIEW_TAG = '[IMAGE](preview.jpg)(preview)';

  function compose(caption, imageTag) {
    const text = String(caption || '').trim();
    const complete = global.KunigamiImageTags.findAll(text);
    const positions = Array.from(text.matchAll(/\[(?:图片|画像|IMAGE)\](?!\()/g))
      .filter(m => !complete.some(tag => m.index >= tag.start && m.index < tag.end));
    if (positions.length > 1) throw new Error(t('一张图片只能指定一个位置，请删除多余的 [图片]。'));
    const at = positions[0];
    const before = (at ? text.slice(0, at.index) : text).trimEnd();
    const after = (at ? text.slice(at.index + at[0].length) : '').trimStart();
    // Space separates the delimiter from URLs; never rewrite slashes within text/tags.
    const beforeIsUrl = /https?:\/\/\S+$/.test(before);
    const left = before ? before + (before.endsWith('/') && !beforeIsUrl ? ' ' : ' / ') : '';
    const right = after ? (after.startsWith('/') ? ' ' : ' / ') + after : '';
    return left + imageTag + right;
  }

  function prepareTranslation(source) {
    source = String(source || '').trim();
    compose(source, PREVIEW_TAG); // Validate image positions before requesting a translation.
    let prefix = '__SAKI_CAPTION_';
    while (source.includes(prefix)) prefix += 'X_';
    const literals = [], tokens = [];
    const tags = new Map(global.KunigamiImageTags.findAll(source).map(tag => [tag.start, tag]));
    let text = '';
    for (let index = 0; index < source.length;) {
      const tag = tags.get(index);
      const match = tag ? tag.raw : source.slice(index).match(/^(?:https?:\/\/[^\s<>\[\]]+|\[(?:图片|画像|IMAGE)\](?!\()|\/)/)?.[0];
      if (match) {
        const token = prefix + tokens.length + '__';
        tokens.push(token); literals.push(match); text += token; index += match.length;
      } else { text += source[index++]; }
    }
    const pattern = new RegExp(prefix + '\\d+__', 'g');
    const hasText = Boolean(text.replace(pattern, '').trim());
    return {text, tokens, hasText, restore(result) {
      const invalid = () => new Error(t('译文中的图片位置或分隔符发生变化，请重试。'));
      if (typeof result !== 'string' || !result.trim()) throw invalid();
      const found = result.match(pattern) || [];
      if (found.length !== tokens.length || found.some((token, index) => token !== tokens[index])) throw invalid();
      const plain = result.replace(pattern, '');
      if ((hasText && !plain.trim()) || /\/|\[(?:图片|画像|IMAGE)\]/.test(plain) || plain.includes(prefix)) throw invalid();
      // Replace in one pass so literal text can never become another replacement token.
      const restored = result.replace(pattern, token => literals[tokens.indexOf(token)]).trim();
      compose(restored, PREVIEW_TAG);
      return restored;
    }};
  }

  function create(options) {
    const el = id => document.getElementById(id);
    const modal = el('imageDescriptionModal'), fields = el('imageComposerFields');
    const prompt = el('imageVisionPrompt'), caption = el('imageCaption'), description = el('imageDescriptionInput');
    const confirm = el('imageDescriptionConfirm'), cancel = el('imageDescriptionCancel');
    const status = el('imageComposerStatus'), fallback = el('imageManualFallback');
    const translationRow = el('imageCaptionTranslationRow'), translationInput = el('imageCaptionTranslationInput');
    const translationButton = el('imageCaptionTranslationButton');
    const savedPrompts = global.SakiVisionPrompts?.attach(prompt, el('imagePromptPresets'));
    let state = null, busy = false, previousFocus = null;

    function notice(message = '', error = false) {
      status.textContent = message;
      status.classList.toggle('error', error);
    }
    function setBusy(value) {
      busy = value;
      fields.disabled = value;
      cancel.disabled = value;
      updateTranslationUi();
    }
    function updateTranslationUi() {
      const translating = Boolean(state?.translation);
      translationButton.disabled = busy || translating;
      translationButton.textContent = t(translating ? '翻译中...' : '翻译');
      confirm.disabled = busy || translating;
      modal.setAttribute('aria-busy', String(busy || translating));
    }
    function setMode(mode) {
      state.mode = mode;
      modal.querySelectorAll('.image-description-mode').forEach(button => {
        button.classList.toggle('active', button.dataset.mode === mode);
        button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
      });
      el('imageManualPanel').hidden = mode !== 'manual';
      el('imagePromptPanel').hidden = mode !== 'ai';
      el('imageDescriptionHelp').textContent = t(mode === 'ai'
        ? 'AI 会自动理解图片内容，此操作会消耗较多 Token。'
        : '手动描述最多4000字，仅供角色理解，不显示在图片气泡上。');
      if (mode === 'manual') { fallback.hidden = true; description.focus(); }
    }
    function preview() {
      const target = el('imageMessagePreview');
      target.replaceChildren();
      try {
        const message = compose(caption.value, PREVIEW_TAG);
        for (const segment of options.splitBubbles(message)) {
          const bubble = document.createElement('div');
          if (segment === PREVIEW_TAG) {
            const image = document.createElement('img');
            image.src = state.url; image.alt = t('待发送图片预览'); bubble.append(image);
          } else { bubble.textContent = segment; }
          target.append(bubble);
        }
      } catch (error) { target.textContent = error.message; }
    }
    function close() {
      if (!state) return;
      state.translation?.controller.abort();
      URL.revokeObjectURL(state.url);
      state = null;
      modal.classList.remove('show');
      el('imageDescriptionPreview').removeAttribute('src');
      el('imageMessagePreview').replaceChildren();
      previousFocus?.focus();
    }
    function open(file) {
      if (busy || state || !options.canSend()) return;
      previousFocus = document.activeElement;
      state = {file, url: URL.createObjectURL(file), mode: 'ai', uploaded: null, draft: options.getDraft(),
        source: options.getDraftSource?.() || null, translation: null};
      state.translatedCaption = state.source ? state.draft : null;
      prompt.value = ''; savedPrompts?.reset(); savedPrompts?.load(); description.value = ''; caption.value = state.draft;
      translationInput.value = '';
      const translationSettings = options.translationSettings();
      translationRow.hidden = !translationSettings.enabled;
      translationInput.placeholder = translationSettings.placeholder;
      updateTranslationUi();
      fallback.hidden = true;
      el('imageDescriptionPreview').src = state.url;
      el('imagePreviewSection').open = false;
      setMode('ai'); notice(); preview();
      modal.classList.add('show');
      options.resizeTranslationInput(translationInput);
      modal.querySelector('.image-composer-body').scrollTop = 0;
      modal.querySelector('[role="dialog"]').focus();
    }

    el('imageInsertPosition').onclick = () => {
      caption.setRangeText('[图片]', caption.selectionStart, caption.selectionEnd, 'end');
      caption.focus(); caption.dispatchEvent(new Event('input', {bubbles: true}));
    };
    caption.addEventListener('input', () => {
      if (!state) return;
      state.source = null; state.translatedCaption = null;
      preview();
    });
    translationInput.addEventListener('input', () => options.resizeTranslationInput(translationInput));
    translationInput.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault(); translationButton.click();
      }
    });
    translationButton.onclick = async () => {
      if (busy || !state || state.translation || translationRow.hidden) return;
      const source = translationInput.value.trim();
      if (!source) return;
      const session = state, draft = caption.value;
      const request = {controller: new AbortController()};
      try {
        const prepared = prepareTranslation(source);
        if (!prepared.hasText) { notice(t('没有可翻译的文字')); return; }
        session.translation = request; updateTranslationUi(); notice();
        const raw = await options.translate(prepared.text, {protectedTokens: prepared.tokens, signal: request.controller.signal});
        if (state !== session || session.translation !== request) return;
        const result = prepared.restore(raw);
        if ((caption.value || caption.value !== draft) && !global.confirm(t('用翻译结果替换当前草稿？'))) return;
        caption.value = result;
        session.source = source; session.translatedCaption = result;
        if (translationInput.value.trim() === source) translationInput.value = '';
        options.resizeTranslationInput(translationInput);
        preview(); notice();
      } catch (error) {
        if (state === session) notice(error.message || t('翻译失败，请重试'), true);
      } finally {
        if (state === session) { session.translation = null; updateTranslationUi(); }
      }
    };
    modal.querySelectorAll('.image-description-mode').forEach(button => {
      button.onclick = () => { if (!busy) { setMode(button.dataset.mode); notice(); } };
    });
    fallback.onclick = () => { setMode('manual'); notice(); };
    cancel.onclick = () => { if (!busy) close(); };
    modal.onclick = event => { if (event.target === modal && !busy) close(); };
    document.addEventListener('keydown', event => {
      // Disabling the focused translation button can move focus to <body>.
      if (state && event.key === 'Escape') { event.preventDefault(); if (!busy) close(); }
    });
    modal.addEventListener('keydown', event => {
      if (event.key === 'Tab') {
        const nodes = Array.from(modal.querySelectorAll('button, textarea, select, input, summary, [tabindex="0"]'))
          .filter(node => !node.matches(':disabled') && node.getClientRects().length);
        const index = nodes.indexOf(document.activeElement);
        if (nodes.length && ((event.shiftKey && index <= 0) || (!event.shiftKey && (index < 0 || index === nodes.length - 1)))) {
          event.preventDefault(); nodes[event.shiftKey ? nodes.length - 1 : 0].focus();
        }
      }
    });
    confirm.onclick = async () => {
      if (busy || !state || state.translation) return;
      if (!options.canSend()) { notice(t('请等待当前消息发送完成。'), true); return; }
      const session = state;
      try {
        compose(caption.value, PREVIEW_TAG);
        const detail = description.value.trim(), instruction = prompt.value.trim();
        if (state.mode === 'manual' && !detail) throw new Error(t('请填写图片描述'));
        if (state.mode === 'manual' && Array.from(detail).length > 4000) throw new Error(t('图片描述不得超过4000字'));
        if (state.mode === 'ai' && Array.from(instruction).length > 2000) throw new Error(t('总结要求不得超过2000字'));
        setBusy(true); options.lock(true); fallback.hidden = true;
        const key = JSON.stringify([state.mode, state.mode === 'ai' ? instruction : detail]);
        if (!state.uploaded || state.uploaded.key !== key) {
          notice(t(state.mode === 'ai' ? '正在上传并识别图片…' : '正在上传图片…'));
          const body = new FormData();
          body.append('file', state.file);
          body.append('description_mode', state.mode);
          if (state.mode === 'ai') body.append('vision_prompt', instruction);
          else body.append('description', detail);
          const response = await options.fetch('/api/vision/upload', {method: 'POST', body});
          const data = await response.json();
          if (!response.ok || data.status !== 'success') {
            if (data.code === 'vision_failed') fallback.hidden = false;
            throw new Error(data.error || t('图片上传失败'));
          }
          state.uploaded = {key, data};
        }
        const data = state.uploaded.data;
        const message = compose(caption.value, `[IMAGE](${data.path})(${data.description})`);
        notice(t('正在发送图片消息…'));
        await options.send(message, () => {
          options.clearDraft(session.draft);
          close();
        }, session.translatedCaption === caption.value ? session.source : null);
      } catch (error) { if (state === session) notice(error.message || t('图片发送失败，请重试。'), true); }
      finally { setBusy(false); options.lock(false); }
    };
    return Object.freeze({open, isOpen: () => !!state});
  }
  global.SakiImageComposer = Object.freeze({compose, prepareTranslation, create});
})(window);
