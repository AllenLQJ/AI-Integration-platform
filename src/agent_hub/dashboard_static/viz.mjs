// Shared chart primitives for the dashboard: palette roles, formatting and small DOM/SVG
// builders used by the overview, the sidebar and the chain views.
// Colours come from --viz-* tokens in style.css (validated light and dark); every
// coloured mark also carries an icon, a text label or an accessible name, never colour alone.

export const SVG = 'http://www.w3.org/2000/svg';

// Stack order keeps success and failure apart (red/green are hard to separate for CVD).
export const OUTCOMES = [
  {key: 'succeeded', label: '成功', icon: '✓', color: 'var(--viz-ok)'},
  {key: 'other', label: '其他结束', icon: '–', color: 'var(--viz-other)'},
  {key: 'failed', label: '失败', icon: '✕', color: 'var(--viz-fail)'},
  {key: 'active', label: '进行中', icon: '◐', color: 'var(--viz-active)'},
];

const TERMINAL = new Set(['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted', 'delivery_unknown']);
/** The outcome (colour, icon, label) an execution state is drawn with. */
export function stateOutcome(state) {
  const key = !TERMINAL.has(state) ? 'active' : state === 'succeeded' ? 'succeeded'
    : state === 'failed' || state === 'timed_out' ? 'failed' : 'other';
  return OUTCOMES.find(outcome => outcome.key === key);
}

export const AGENT_SLOTS = 8;

export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  if (hours < 24) return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
}

export function relativeTime(timestamp, now = Date.now() / 1000) {
  const seconds = Math.max(0, now - timestamp);
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

/** Colour follows the executor, not its rank: configured order first, then names as first seen. */
export function agentColors(configured = [], seen = []) {
  const order = [...configured];
  for (const name of seen) if (!order.includes(name)) order.push(name);
  return new Map(order.map((name, index) => [name, index < AGENT_SLOTS ? `var(--viz-agent-${index + 1})` : 'var(--viz-other)']));
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}
export function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}
export function swatch(color, icon) {
  const mark = el('span', 'viz-swatch', icon);
  mark.style.background = color;
  mark.setAttribute('aria-hidden', 'true');
  return mark;
}
export function legend(items) {
  const list = el('ul', 'viz-legend');
  for (const item of items) { const entry = el('li'); entry.append(swatch(item.color, item.icon), el('span', '', item.label)); list.append(entry); }
  return list;
}
export function card(title, note) {
  const box = el('section', 'viz-card');
  const head = el('div', 'viz-card-head');
  head.append(el('h2', '', title));
  if (note) head.append(el('span', 'viz-note', note));
  box.append(head);
  return box;
}
/** One tooltip per card; marks call show() on hover and focus. It is fixed to the viewport, so
 * panels that hide their overflow cannot clip it; it flips and clamps to stay on screen. */
// One tooltip element shared by every chart, living directly under <body>: glass panels use
// backdrop-filter, which would otherwise turn a fixed tooltip inside them into a clipped one.
let sharedTip = null;
export function tooltip(box) {
  const body = globalThis.document?.body;
  let tip;
  if (body) {
    if (!sharedTip) { sharedTip = el('div', 'viz-tip'); sharedTip.setAttribute('aria-hidden', 'true'); body.append(sharedTip); }
    tip = sharedTip;
  } else {
    tip = el('div', 'viz-tip');
    tip.setAttribute('aria-hidden', 'true');
    box.append(tip);
  }
  // A chart being (re)drawn replaces the marks a visible tooltip pointed at.
  tip.hidden = true;
  return {
    show(lines, event) {
      tip.replaceChildren(...lines.map((line, i) => el(i ? 'div' : 'strong', '', line)));
      tip.hidden = false;
      if (event?.clientX != null) {
        const width = globalThis.innerWidth || 1024, height = globalThis.innerHeight || 768, tall = tip.offsetHeight || 0;
        const {clientX: x, clientY: y} = event;
        tip.style.left = `${Math.max(4, Math.min(x + 12, width - 190))}px`;
        tip.style.top = `${y + 12 + tall > height && y - 12 - tall >= 0 ? y - 12 - tall : y + 12}px`;
      } else {
        // Keyboard focus has no pointer: anchor to the focused mark.
        const rect = event?.target?.getBoundingClientRect?.();
        if (rect) { tip.style.left = `${Math.max(4, rect.left)}px`; tip.style.top = `${rect.bottom + 6}px`; }
      }
    },
    hide() { tip.hidden = true; },
  };
}
export function hover(target, tip, lines) {
  const show = event => tip.show(lines(), event);
  target.addEventListener('pointermove', show);
  target.addEventListener('focus', show);
  target.addEventListener('pointerleave', () => tip.hide());
  target.addEventListener('blur', () => tip.hide());
}
export function dataTable(caption, headers, rows) {
  const details = el('details', 'viz-data');
  details.append(el('summary', '', '查看数据'));
  const table = el('table');
  const head = el('tr');
  for (const name of headers) head.append(el('th', '', name));
  const thead = el('thead'); thead.append(head);
  const tbody = el('tbody');
  for (const row of rows) { const tr = el('tr'); for (const cell of row) tr.append(el('td', '', cell)); tbody.append(tr); }
  table.append(el('caption', '', caption), thead, tbody);
  details.append(table);
  return details;
}
