// ⌘K command palette: jump to any chain or run a view action from the keyboard.

/**
 * How well `query` matches `text`: a substring scores by how early it starts; otherwise the
 * query's characters must appear in order, and tighter matches score higher. null = no match.
 */
export function score(query, text) {
  const q = String(query || '').trim().toLowerCase(), t = String(text || '').toLowerCase();
  if (!q) return 0;
  const direct = t.indexOf(q);
  if (direct >= 0) return 1000 - direct;
  let from = 0, last = -1, gaps = 0;
  for (const ch of q) {
    if (ch === ' ') continue;
    const at = t.indexOf(ch, from);
    if (at < 0) return null;
    if (last >= 0) gaps += at - last - 1;
    last = at; from = at + 1;
  }
  return 500 - gaps;
}

/** Items matching `query` on title or id, best first; ties keep their original order. */
export function rank(items, query, limit = 8) {
  return items
    .map((item, index) => {
      const best = Math.max(score(query, item.title) ?? -Infinity, score(query, item.id) ?? -Infinity);
      return {item, index, best};
    })
    .filter(entry => entry.best > -Infinity)
    .sort((a, b) => b.best - a.best || a.index - b.index)
    .slice(0, limit)
    .map(entry => entry.item);
}

/**
 * Build the palette. `items(query)` returns the local candidates ({id, title, hint, group, run});
 * `search(query)` may return more from the server. Returns {open, close, isOpen}.
 */
export function createPalette({items, search = null, doc = globalThis.document, debounce = 180} = {}) {
  const el = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  };
  const backdrop = el('div', 'cmd-backdrop');
  backdrop.hidden = true;
  const panel = el('div', 'cmd-panel');
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-label', '命令面板');
  const input = el('input', 'cmd-input');
  input.type = 'text';
  input.placeholder = '搜索任务链，或输入命令…';
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-controls', 'cmd-list');
  input.setAttribute('aria-expanded', 'true');
  input.setAttribute('aria-autocomplete', 'list');
  input.autocomplete = 'off';
  const list = el('ul', 'cmd-list');
  list.id = 'cmd-list';
  list.setAttribute('role', 'listbox');
  const foot = el('div', 'cmd-foot', '↑↓ 选择 · ↵ 打开 · Esc 关闭');
  panel.append(input, list, foot);
  backdrop.append(panel);
  doc.body?.append(backdrop);

  let shown = [], active = 0, timer = null, returnFocus = null, generation = 0;
  function render(entries) {
    shown = entries;
    active = Math.min(active, Math.max(0, shown.length - 1));
    list.replaceChildren();
    shown.forEach((entry, index) => {
      const option = el('li', 'cmd-option');
      option.id = `cmd-opt-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(index === active));
      option.append(el('span', 'cmd-group', entry.group || '命令'), el('span', 'cmd-title', entry.title), el('span', 'cmd-hint', entry.hint || ''));
      option.addEventListener('mousemove', () => { if (active !== index) { active = index; mark(); } });
      option.addEventListener('click', () => run(index));
      list.append(option);
    });
    if (!shown.length) list.append(el('li', 'cmd-empty', '没有匹配的任务链或命令'));
    mark();
  }
  function mark() {
    [...list.children].forEach((child, index) => child.setAttribute?.('aria-selected', String(index === active)));
    if (shown.length) input.setAttribute('aria-activedescendant', `cmd-opt-${active}`);
    else input.removeAttribute?.('aria-activedescendant');
    list.children[active]?.scrollIntoView?.({block: 'nearest'});
  }
  function refresh() {
    const query = input.value;
    render(rank(items(query), query));
    clearTimeout(timer);
    if (!search || !query.trim()) return;
    const ticket = ++generation;
    timer = setTimeout(async () => {
      try {
        const extra = await search(query);
        if (ticket !== generation || backdrop.hidden) return;
        const seen = new Set(shown.map(entry => entry.id));
        render(rank([...shown, ...extra.filter(entry => !seen.has(entry.id))], query));
      } catch { /* the local results stay */ }
    }, debounce);
  }
  function run(index) {
    const entry = shown[index];
    if (!entry) return;
    close();
    entry.run();
  }
  function open() {
    if (!backdrop.hidden) return;
    returnFocus = doc.activeElement;
    backdrop.hidden = false;
    input.value = '';
    active = 0;
    refresh();
    input.focus?.();
  }
  function close() {
    if (backdrop.hidden) return;
    backdrop.hidden = true;
    clearTimeout(timer);
    generation += 1;
    returnFocus?.focus?.();
    returnFocus = null;
  }
  input.addEventListener('input', () => { active = 0; refresh(); });
  input.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault?.();
      if (shown.length) { active = (active + (event.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length; mark(); }
    } else if (event.key === 'Enter') {
      event.preventDefault?.();
      run(active);
    } else if (event.key === 'Escape') {
      event.preventDefault?.();
      event.stopPropagation?.();
      close();
    } else if (event.key === 'Tab') {
      event.preventDefault?.(); // focus stays in the palette
    }
  });
  backdrop.addEventListener('mousedown', event => { if (event.target === backdrop) close(); });
  return {open, close, isOpen: () => !backdrop.hidden, element: backdrop};
}
