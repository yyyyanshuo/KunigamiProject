(function(global) {
  'use strict';
  const t = (text, values) => global.SakiI18n.t(text, values);
  function create() {
    const el = id => document.getElementById(id);
    const mask = el('momentVisibilityMask'), dialog = mask.querySelector('[role="dialog"]');
    const list = el('momentVisibilityList'), status = el('momentVisibilityStatus');
    const apply = el('momentVisibilityApply'), cancel = el('momentVisibilityCancel');
    const opener = el('momentVisibilityOpen');
    let selected = new Set(), draft = new Set(), previousFocus, requestId = 0, readonly = false;
    function updateSummary() {
      el('momentVisibilitySummary').textContent = selected.size
        ? t('已屏蔽 {count} 位角色', {count:selected.size}) : t('所有角色可见');
    }
    function close() {
      ++requestId;
      mask.hidden = true;
      previousFocus?.focus();
    }
    function render(rows) {
      list.replaceChildren();
      if (!rows.length) { status.textContent = t('暂无可选角色'); return; }
      rows.forEach(row => {
        const item = document.createElement(readonly ? 'div' : 'label');
        item.className = 'moment-visibility-person';
        const avatar = document.createElement('img');
        avatar.src = global.getCosUrl ? global.getCosUrl(row.avatar || '') : row.avatar || '/static/default_avatar.png';
        avatar.alt = ''; avatar.onerror = () => { avatar.onerror = null; avatar.src = '/static/default_avatar.png'; };
        const name = document.createElement('span'); name.className = 'moment-visibility-name';
        const label = document.createElement('strong'); label.textContent = row.name || row.id;
        const id = document.createElement('small'); id.textContent = row.id;
        name.append(label, id); item.append(avatar, name);
        if (!readonly) {
          const check = document.createElement('input'); check.type = 'checkbox'; check.value = row.id;
          check.checked = draft.has(row.id);
          check.onchange = () => { check.checked ? draft.add(row.id) : draft.delete(row.id); };
          item.append(check);
        }
        list.append(item);
      });
    }
    async function open(rows) {
      readonly = Array.isArray(rows);
      previousFocus = document.activeElement;
      const currentRequest = ++requestId;
      draft = new Set(selected);
      el('momentVisibilityTitle').textContent = t(readonly ? '部分用户不可见' : '谁不可见');
      el('momentVisibilityHint').textContent = t(readonly ? '以下角色无法看到这条朋友圈，也不能参与点赞或评论。' : '勾选不希望看到这条朋友圈的角色，仅对本条生效。');
      apply.hidden = readonly; apply.disabled = true;
      cancel.textContent = t(readonly ? '关闭' : '取消');
      list.replaceChildren(); status.textContent = '';
      mask.hidden = false; dialog.focus();
      if (readonly) { render(rows); return; }
      status.textContent = t('正在加载角色…');
      try {
        const response = await fetch('/api/moments/characters?details=1');
        if (!response.ok) throw new Error();
        const characters = await response.json();
        if (!Array.isArray(characters)) throw new Error();
        if (currentRequest !== requestId) return;
        draft = new Set([...draft].filter(id => characters.some(row => row.id === id)));
        status.textContent = ''; render(characters); apply.disabled = false;
      } catch (_) {
        if (currentRequest === requestId) status.textContent = t('角色列表加载失败，请关闭后重试。');
      }
    }
    opener.onclick = () => open();
    cancel.onclick = close;
    apply.onclick = () => { selected = new Set(draft); updateSummary(); close(); };
    mask.onclick = event => { if (event.target === mask) close(); };
    mask.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
      if (event.key === 'Tab') {
        const nodes = [...dialog.querySelectorAll('button, input')].filter(node => !node.disabled && node.getClientRects().length);
        const index = nodes.indexOf(document.activeElement);
        if (nodes.length && ((event.shiftKey && index <= 0) || (!event.shiftKey && (index < 0 || index === nodes.length-1)))) {
          event.preventDefault(); nodes[event.shiftKey ? nodes.length-1 : 0].focus();
        }
      }
    });
    return {getSelected: () => [...selected], reset: () => {selected.clear(); updateSummary();}, show: rows => open(rows)};
  }
  global.SakiMomentVisibility = Object.freeze({create});
})(window);
