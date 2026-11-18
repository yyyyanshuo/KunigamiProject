(function(global) {
  'use strict';
  const accountId = value => /^[1-9]\d*$/.test(String(value)) ? String(value) : '';
  const current = accountId(global.SakiPrivacyUser);
  const translationPrefix = owner => owner ? 'saki:translations:' + owner + ':' : '';
  const translationCachePrefix = translationPrefix(current);

  function storageKeys() {
    return Array.from({length: localStorage.length}, (_, index) => localStorage.key(index));
  }

  function migrateTranslations(previous) {
    if (!storageKeys().some(key => key.startsWith('kt_chat_'))) return;
    // Old results/visibility shared one namespace. Only the recorded owner can
    // claim those entries; source text already contains an explicit account ID.
    const prefix = translationPrefix(accountId(previous));
    function destination(key) {
      const source = /^kt_chat_source_users\/([1-9]\d*)_(.+)$/.exec(key);
      if (source) return translationPrefix(source[1]) + 'source_' + source[2];
      if (prefix && key.startsWith('kt_chat_v2_')) return prefix + 'result_' + key.slice('kt_chat_v2_'.length);
      if (prefix && key === 'kt_chat_hidden') return prefix + 'hidden';
      return null;
    }
    for (const key of storageKeys()) {
      const target = destination(key);
      if (!target) continue;
      try {
        if (localStorage.getItem(target) === null) localStorage.setItem(target, localStorage.getItem(key));
        localStorage.removeItem(key);
      } catch (_) {}
    }
    if (prefix) {
      try {
        const legacy = JSON.parse(localStorage.getItem('kt_chat_trans_lru') || '[]');
        const existing = JSON.parse(localStorage.getItem(prefix + 'lru') || '[]');
        const keys = [...new Set([
          ...(Array.isArray(legacy) ? legacy.filter(key => typeof key === 'string').map(destination) : []),
          ...(Array.isArray(existing) ? existing : []),
        ])].filter(key => typeof key === 'string' && key.startsWith(prefix)
          && key !== prefix + 'hidden' && key !== prefix + 'lru' && localStorage.getItem(key) !== null);
        if (keys.length) localStorage.setItem(prefix + 'lru', JSON.stringify(keys));
        localStorage.removeItem('kt_chat_trans_lru');
      } catch (_) {}
    }
  }

  function clearLocalData(deleteTranslations) {
    for (const key of storageKeys()) {
      const savedTranslation = /^saki:translations:[1-9]\d*:/.test(key);
      if (!savedTranslation || deleteTranslations) {
        localStorage.removeItem(key);
      }
    }
  }

  async function clear({preserveTranslations = false} = {}) {
    try { clearLocalData(!preserveTranslations); } catch (_) {}
    try { sessionStorage.clear(); } catch (_) {}
    if ('caches' in global) {
      try { await Promise.all((await caches.keys()).map(key=>caches.delete(key))); } catch (_) {}
    }
  }
  global.SakiPrivacy = Object.freeze({clear, translationCachePrefix});
  document.addEventListener('DOMContentLoaded',()=>{
    for(const id of ['exportResultModal','exportModal']){
      const modal=document.getElementById(id);
      const card=modal?.firstElementChild;
      if(!card)continue;
      const note=document.createElement('p');note.className='privacy-note';
      note.textContent=SakiI18n.t('复制或导出前，请检查其中的人设、关系和对话是否含有不想分享的信息。');
      card.prepend(note);
    }
  });
  // Preserve account-scoped translations through switching and logout. Clear
  // shared drafts and other unscoped caches before another account uses the tab.
  try {
    const previous=localStorage.getItem('saki-cache-owner');
    migrateTranslations(previous);
    if (!current || previous!==current) {
      clearLocalData(false); sessionStorage.clear();
      if ('caches' in global) caches.keys().then(keys=>Promise.all(keys.map(key=>caches.delete(key)))).catch(()=>{});
    }
    if(current)localStorage.setItem('saki-cache-owner',current);
  } catch (_) {}
  document.addEventListener('click', async event=>{
    const link=event.target.closest?.('a[href="/logout"]');
    if (!link) return;
    event.preventDefault(); await clear({preserveTranslations: true}); global.location.href='/logout';
  });
})(window);
