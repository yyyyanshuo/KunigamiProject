(() => {
  'use strict';
  const input = document.getElementById('guide-search-input');
  const clear = document.getElementById('guide-search-clear');
  const results = document.getElementById('guide-search-results');
  const status = document.getElementById('guide-search-status');
  const targets = Array.from(document.querySelectorAll('.doc-section h2, .doc-section h3, .doc-section p, .doc-section li, .doc-section dt, .doc-section dd, .doc-section tr, .doc-section pre'));
  const nav = document.getElementById('guide-top-nav');
  const top = document.getElementById('guide-back-to-top');
  const motion = () => matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth';
  function close() { results.hidden = true; results.replaceChildren(); status.textContent = ''; }
  function render() {
    const query = input.value.trim().toLocaleLowerCase();
    clear.hidden = !query;
    close();
    if (!query) return;
    // Prefer the most specific element so nested paragraphs do not duplicate hits.
    const matches = targets.filter(node => node.textContent.toLocaleLowerCase().includes(query));
    const unique = matches.filter(node => !matches.some(other => other !== node && node.contains(other))).slice(0, 12);
    results.hidden = false;
    if (!unique.length) {
      const empty = document.createElement('p');
      empty.className = 'search-empty';
      empty.textContent = results.dataset.empty;
      results.append(empty);
      status.textContent = empty.textContent;
      return;
    }
    status.textContent = String(unique.length);
    for (const target of unique) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'search-result';
      const title = document.createElement('span');
      title.className = 'search-result-title';
      title.textContent = target.closest('.doc-section').querySelector('h2').textContent;
      const snippet = document.createElement('span');
      snippet.className = 'search-result-text'; snippet.textContent = target.textContent.trim();
      button.append(title, snippet);
      button.addEventListener('click', () => {
        close();
        target.tabIndex = -1;
        target.focus({preventScroll: true});
        target.scrollIntoView({behavior: motion(), block: 'center'});
        target.classList.add('search-hit');
        setTimeout(() => target.classList.remove('search-hit'), 2000);
      });
      results.append(button);
    }
  }
  input.addEventListener('input', render);
  input.addEventListener('focus', render);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); results.querySelector('button')?.click(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('button')?.focus(); }
    if (event.key === 'Escape') { close(); input.blur(); }
  });
  clear.addEventListener('click', () => { input.value = ''; render(); input.focus(); });
  document.addEventListener('click', event => {
    if (!event.target.closest('.guide-search')) close();
    if (!event.target.closest('#guide-top-nav')) nav.open = false;
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { close(); nav.open = false; }
  });
  nav.querySelectorAll('a').forEach(link => link.addEventListener('click', () => { nav.open = false; }));
  function updateTop() { top.hidden = window.scrollY < 480; }
  window.addEventListener('scroll', updateTop, {passive: true});
  top.addEventListener('click', () => {
    document.querySelector('h1').tabIndex = -1;
    document.querySelector('h1').focus({preventScroll: true});
    window.scrollTo({top: 0, behavior: motion()});
  });
  updateTop();
})();
