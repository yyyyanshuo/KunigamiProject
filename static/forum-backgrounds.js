(function (global) {
    'use strict';
    let selected = [];
    let sources = [];
    let loading = false;
    let loaded = false;
    const el = id => document.getElementById(id);
    const t = text => global.SakiI18n.t(text);
    const copy = value => JSON.parse(JSON.stringify(value));
    const kinds = {user: '用户设定', character: '角色设定', saved: '已保存背景'};

    function configFor(forum) {
        return copy(forum.background_config || {items: [], note: forum.supplementary_info || ''});
    }

    function capture() {
        return {
            items: copy(selected), note: el('supplementaryInput').value,
            mode: el('backgroundMode').value, name: el('backgroundName').value,
        };
    }

    function request() {
        const value = capture();
        if (value.mode !== 'save') value.name = '';
        return value;
    }

    function restore(value) {
        selected = copy(value.items || []);
        el('supplementaryInput').value = value.note || '';
        el('backgroundMode').value = value.mode === 'save' ? 'save' : 'once';
        el('backgroundName').value = value.name || '';
        render();
    }

    function loadForum(forum) {
        restore({...configFor(forum), mode: 'once', name: ''});
    }

    function useResult(forum) {
        // A temporary selection is consumed by this generation, not the next one.
        loadForum(forum);
        if (forum.saved_background) {
            sources = [forum.saved_background, ...sources.filter(s => s.id !== forum.saved_background.id)];
            render();
        }
    }

    function textNode(tag, text, className) {
        const node = document.createElement(tag);
        node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function setSelected(source, checked) {
        selected = selected.filter(item => item.id !== source.id);
        if (checked) selected.push(copy(source));
        render();
    }

    function card(source, isSelectedList) {
        const chosen = selected.find(item => item.id === source.id);
        const visible = chosen || source;
        const details = document.createElement('details');
        details.className = 'background-card';
        details.dataset.backgroundKey = (isSelectedList ? 'selected:' : 'source:') + source.id;
        const summary = document.createElement('summary');
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = Boolean(chosen);
        check.setAttribute('aria-label', visible.label || t(kinds[visible.kind]));
        check.addEventListener('click', event => event.stopPropagation());
        check.addEventListener('change', () => setSelected(source, check.checked));
        summary.append(check, textNode('span', visible.label || t(kinds[visible.kind])));
        summary.append(textNode('small', t(kinds[visible.kind])));
        details.append(summary, textNode('pre', visible.content, 'background-preview'));
        const current = sources.find(item => item.id === visible.id);
        if (isSelectedList && current && (current.content !== visible.content || current.label !== visible.label)) {
            const update = textNode('button', t('更新为当前设定'), 'background-action');
            update.type = 'button';
            update.addEventListener('click', () => {
                selected = selected.map(item => item.id === current.id ? copy(current) : item);
                render();
            });
            details.append(update);
        }
        return details;
    }

    function render() {
        const open = new Set(Array.from(document.querySelectorAll('[data-background-key][open]'), node => node.dataset.backgroundKey));
        const selection = el('backgroundSelection');
        selection.replaceChildren();
        if (selected.length) selected.forEach(item => selection.append(card(item, true)));
        else selection.append(textNode('div', t('尚未选择背景，可从下方多选。'), 'background-hint'));
        const list = el('backgroundSources');
        list.replaceChildren();
        for (const [kind, label] of Object.entries(kinds)) {
            const group = document.createElement('details');
            group.className = 'background-source-group';
            group.dataset.backgroundKey = 'group:' + kind;
            const matches = sources.filter(item => item.kind === kind);
            group.append(textNode('summary', t(label) + ' · ' + matches.length));
            matches.forEach(item => group.append(card(item, false)));
            if (!matches.length) group.append(textNode('div', t('暂无可载入的内容'), 'background-hint'));
            list.append(group);
        }
        document.querySelectorAll('[data-background-key]').forEach(node => {
            node.open = open.has(node.dataset.backgroundKey);
        });
        el('backgroundNameRow').hidden = el('backgroundMode').value !== 'save';
        el('backgroundModeHint').textContent = t(el('backgroundMode').value === 'save'
            ? '生成成功后保存当前组合，后续续写自动沿用。'
            : '仅用于本轮生成，不覆盖此论坛已保存的背景。');
        el('backgroundReload').disabled = loading;
    }

    async function reload() {
        if (loading) return;
        loading = true;
        el('backgroundStatus').textContent = t('正在载入背景来源…');
        render();
        try {
            const response = await fetch('/api/forums/background-sources');
            if (!response.ok) throw new Error('Background sources unavailable');
            const data = await response.json();
            sources = data.sources;
            loaded = true;
            el('backgroundStatus').textContent = '';
        } catch (_) {
            el('backgroundStatus').textContent = t('背景来源加载失败，请重试。');
        } finally {
            loading = false;
            render();
        }
    }

    function init() {
        el('backgroundMode').addEventListener('change', render);
        el('backgroundReload').addEventListener('click', reload);
        render();
        if (!loaded) reload();
    }

    global.ForumBackgrounds = Object.freeze({init, capture, request, restore, loadForum, useResult});
})(window);
