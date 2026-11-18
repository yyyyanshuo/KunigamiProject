(function(global) {
  'use strict';
  const t = key => global.SakiI18n.t(key);
  function attach(input, container) {
    const select = document.createElement('select');
    select.setAttribute('aria-label', t('已保存的识图提示词'));
    const save = document.createElement('button'), remove = document.createElement('button');
    save.type = remove.type = 'button';
    save.textContent = t('保存提示词'); remove.textContent = t('删除所选');
    const status = document.createElement('span'); status.setAttribute('role', 'status');
    container.append(select, save, remove, status);
    let busy = false, sequence = 0;
    function render(prompts, chosen = '') {
      select.replaceChildren(new Option(t('选择已保存的提示词'), ''));
      for (const prompt of prompts) select.add(new Option(prompt, prompt));
      select.value = chosen; remove.disabled = !select.value;
    }
    render([]);
    select.onchange = () => {
      remove.disabled = !select.value;
      if (select.value) { input.value = select.value; input.dispatchEvent(new Event('input', {bubbles:true})); }
    };
    async function request(action) {
      if (busy) return;
      const prompt = action === 'delete' ? select.value : input.value.trim();
      if (action && !prompt) { status.textContent = t('请填写识图提示词'); return; }
      busy = true; const ticket = ++sequence;
      select.disabled = save.disabled = remove.disabled = true;
      status.textContent = '';
      try {
        const response = await fetch('/api/vision/prompts', action ? {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({action, prompt})
        } : {cache:'no-store'});
        const data = await response.json();
        if (!response.ok || !Array.isArray(data.prompts)) throw new Error(t(data.error || '加载失败'));
        if (ticket !== sequence) return;
        render(data.prompts, action === 'add' ? prompt : '');
        if (action) status.textContent = t(action === 'add' ? '提示词已保存' : '提示词已删除');
      } catch (error) { status.textContent = error.message || t('加载失败'); }
      finally { busy = false; select.disabled = save.disabled = false; remove.disabled = !select.value; }
    }
    save.onclick = () => request('add'); remove.onclick = () => request('delete');
    return {load: () => request(), reset: () => { select.value = ''; remove.disabled = true; status.textContent = ''; }};
  }
  global.SakiVisionPrompts = Object.freeze({attach});
})(window);
