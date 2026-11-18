(function(global) {
  'use strict';
  const catalog = global.SakiUICatalog || {};
  function t(key, values) {
    let result = Object.prototype.hasOwnProperty.call(catalog,key) ? catalog[key] : key;
    result = result.replace(/\{(\w+)\}/g,(match,name)=>Object.prototype.hasOwnProperty.call(values||{},name)?String(values[name]):match);
    if (!Object.prototype.hasOwnProperty.call(catalog,key) && key.includes("\n")) return key.split("\n").map(line=>t(line,values)).join("\n");
    return result;
  }
  function fragment(source) {
    // Only called with authored literal fragments; values are never walked or translated.
    return source.split(/(<[^>]*>)/).map(piece => {
      if (piece.startsWith('<')) return piece.replace(/(placeholder|title|aria-label|alt)="([^"<>]*)"/g,
        (_,name,value)=>name+'="'+t(value).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;')+'"');
      const trimmed=piece.trim();
      return Object.prototype.hasOwnProperty.call(catalog,trimmed) ? piece.replace(trimmed,t(trimmed)) : piece;
    }).join('');
  }
  function ui(strings,...values) {
    const key=strings.reduce((acc,text,i)=>acc+text+(i<values.length?'{'+i+'}':''),'');
    if (Object.prototype.hasOwnProperty.call(catalog,key)) return t(key,Object.fromEntries(values.map((v,i)=>[i,v])));
    // Assemble literal fragments before translating: an interpolation may split a tag.
    // Substitute values afterwards so user text is never passed through the catalogue.
    const literal=strings.reduce((acc,text,i)=>acc+text+(i<values.length?'\uE000'+i+'\uE001':''),'');
    return fragment(literal).replace(/\uE000(\d+)\uE001/g,(_,i)=>String(values[Number(i)]));
  }
  function normalizeLanguage(value) {
    const text=String(value || '').trim().toLowerCase().replace(/_/g,'-');
    const aliases={'japanese':'ja','日本語':'ja','日语':'ja','日文':'ja','english':'en','英语':'en','英文':'en','chinese':'zh','中文':'zh','汉语':'zh','简体中文':'zh'};
    return aliases[text] || (/^(ja|en|zh)(-|$)/.test(text) ? text.split('-')[0] : text);
  }
  function needsSendTranslation(target) {
    const ui=document.documentElement.lang;
    return normalizeLanguage(target || ui) !== normalizeLanguage(ui);
  }
  async function setLanguage(language, select) {
    if (select) select.disabled = true;
    try {
      const response = await fetch('/api/ui/language',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_language:language})});
      if (!response.ok) throw new Error(t('服务暂时不可用，请稍后重试。'));
      // All shortcuts resolve to the same server preference; reload preserves rendered content.
      global.location.reload();
    } catch (_) {
      global.alert(t('服务暂时不可用，请稍后重试。'));
      if (select) {select.disabled=false;select.value=document.documentElement.lang;}
    }
  }
  if ('serviceWorker' in navigator) {
    const notify = () => navigator.serviceWorker.controller?.postMessage({type:'saki-ui-language',language:document.documentElement.lang});
    navigator.serviceWorker.addEventListener('controllerchange',notify);
    notify();
  }
  global.SakiI18n = Object.freeze({t,fragment,ui,setLanguage,normalizeLanguage,needsSendTranslation});
})(window);
