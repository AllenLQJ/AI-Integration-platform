// Global overview: KPI tiles, a 14-day stacked bar chart, executor comparison,
// work-status distribution and the chains that need a person.
// Colours come from --viz-* tokens in style.css (validated light and dark); every
// coloured mark also carries an icon and a text label, never colour alone.
import {label} from './status_labels.mjs';

const SVG = 'http://www.w3.org/2000/svg';

// Stack order keeps success and failure apart (red/green are hard to separate for CVD).
export const OUTCOMES = [
  {key: 'succeeded', label: '成功', icon: '✓', color: 'var(--viz-ok)'},
  {key: 'other', label: '其他结束', icon: '–', color: 'var(--viz-other)'},
  {key: 'failed', label: '失败', icon: '✕', color: 'var(--viz-fail)'},
  {key: 'active', label: '进行中', icon: '◐', color: 'var(--viz-active)'},
];

export const WORK_BUCKETS = [
  {key: 'done', label: '验收通过', icon: '✓', color: 'var(--viz-ok)', statuses: ['completed', 'passed']},
  {key: 'plain', label: '成功·未设验收', icon: '○', color: 'var(--viz-plain)', statuses: ['succeeded']},
  {key: 'person', label: '待人工处理', icon: '!', color: 'var(--viz-wait)',
    statuses: ['needs_review', 'rework_required', 'waiting_for_user']},
  {key: 'failed', label: '失败', icon: '✕', color: 'var(--viz-fail)', statuses: ['failed', 'timed_out', 'rejected']},
  {key: 'active', label: '进行中', icon: '◐', color: 'var(--viz-active)',
    statuses: ['queued', 'running', 'cancelling', 'pending', 'awaiting_verification', 'verifying', 'reviewing', 'dispatching']},
  {key: 'other', label: '其他', icon: '–', color: 'var(--viz-other)', statuses: []},
];

export const AGENT_SLOTS = 8;

/** A y-axis maximum on a whole 1/2/5 step (counts are integers) with about `count` gridlines; never zero. */
export function niceScale(max, count = 4) {
  const target = Math.max(1, max) / count;
  const magnitude = 10 ** Math.floor(Math.log10(target));
  const step = Math.max(1, [1, 2, 5, 10].map(m => m * magnitude).find(s => s >= target));
  const top = Math.max(step, Math.ceil(max / step) * step);
  const ticks = [];
  for (let value = 0; value <= top + step / 2; value += step) ticks.push(Math.round(value * 1e6) / 1e6);
  return {max: top, ticks};
}

/** Work-status counts folded into fixed buckets; percents sum to 100 (largest remainder). */
export function workBuckets(workStatus = {}) {
  const known = new Map(WORK_BUCKETS.flatMap(bucket => bucket.statuses.map(status => [status, bucket.key])));
  const counts = Object.fromEntries(WORK_BUCKETS.map(bucket => [bucket.key, 0]));
  for (const [status, count] of Object.entries(workStatus)) counts[known.get(status) || 'other'] += count;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const raw = WORK_BUCKETS.map(bucket => (total ? counts[bucket.key] * 100 / total : 0));
  const percents = raw.map(Math.floor);
  let left = total ? 100 - percents.reduce((a, b) => a + b, 0) : 0;
  for (const index of raw.map((value, i) => [value - Math.floor(value), i]).sort((a, b) => b[0] - a[0]).map(x => x[1])) {
    if (left <= 0) break;
    if (counts[WORK_BUCKETS[index].key]) { percents[index] += 1; left -= 1; }
  }
  return WORK_BUCKETS.map((bucket, i) => ({...bucket, count: counts[bucket.key], percent: percents[i]}));
}

/** Totals over the day window; the success rate counts finished executions only. */
export function windowStats(daily = []) {
  const sum = key => daily.reduce((total, day) => total + (day[key] || 0), 0);
  const succeeded = sum('succeeded'), finished = succeeded + sum('failed') + sum('other');
  return {executions: finished + sum('active'), finished, succeeded,
    rate: finished ? succeeded / finished : null, perDay: daily.length ? (finished + sum('active')) / daily.length : 0};
}

/** Daily success rate, or null for days with nothing finished (drawn as a gap). */
export const dailyRates = (daily = []) => daily.map(day => {
  const finished = (day.succeeded || 0) + (day.failed || 0) + (day.other || 0);
  return finished ? day.succeeded / finished : null;
});

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

export const dayLabel = iso => `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;

/** Colour follows the executor, not its rank: configured order first, then names as first seen. */
export function agentColors(configured = [], seen = []) {
  const order = [...configured];
  for (const name of seen) if (!order.includes(name)) order.push(name);
  return new Map(order.map((name, index) => [name, index < AGENT_SLOTS ? `var(--viz-agent-${index + 1})` : 'var(--viz-other)']));
}

/** Stacked segments for one day, bottom to top, in plot pixels with a 2px gap between fills. */
export function stackDay(day, scaleMax, height, gap = 2) {
  const segments = [];
  let base = height;
  for (const outcome of OUTCOMES) {
    const value = day[outcome.key] || 0;
    if (!value) continue;
    const full = value / scaleMax * height;
    const drawn = Math.max(1, full - (segments.length ? gap : 0));
    segments.push({...outcome, value, y: base - full, height: drawn});
    base -= full;
  }
  return segments;
}

// --- rendering -------------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}
function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}
function swatch(color, icon) {
  const mark = el('span', 'viz-swatch', icon);
  mark.style.background = color;
  mark.setAttribute('aria-hidden', 'true');
  return mark;
}
function legend(items) {
  const list = el('ul', 'viz-legend');
  for (const item of items) { const entry = el('li'); entry.append(swatch(item.color, item.icon), el('span', '', item.label)); list.append(entry); }
  return list;
}
function card(title, note) {
  const box = el('section', 'viz-card');
  const head = el('div', 'viz-card-head');
  head.append(el('h2', '', title));
  if (note) head.append(el('span', 'viz-note', note));
  box.append(head);
  return box;
}
/** One tooltip per card; marks call show() on hover and focus. */
function tooltip(box) {
  const tip = el('div', 'viz-tip');
  tip.hidden = true;
  tip.setAttribute('aria-hidden', 'true');
  box.append(tip);
  return {
    show(lines, event) {
      tip.replaceChildren(...lines.map((line, i) => el(i ? 'div' : 'strong', '', line)));
      tip.hidden = false;
      const rect = box.getBoundingClientRect?.();
      if (rect && event?.clientX != null) {
        tip.style.left = `${Math.min(event.clientX - rect.left + 12, rect.width - 180)}px`;
        tip.style.top = `${event.clientY - rect.top + 12}px`;
      }
    },
    hide() { tip.hidden = true; },
  };
}
function hover(target, tip, lines) {
  const show = event => tip.show(lines(), event);
  target.addEventListener('pointermove', show);
  target.addEventListener('focus', show);
  target.addEventListener('pointerleave', () => tip.hide());
  target.addEventListener('blur', () => tip.hide());
}
function dataTable(caption, headers, rows) {
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

function kpis(data) {
  const stats = windowStats(data.daily), work = data.work_status || {};
  const review = work.needs_review || 0, rework = work.rework_required || 0, waiting = work.waiting_for_user || 0;
  const tiles = el('div', 'kpis');
  const tile = (name, value, sub, extra, tone) => {
    const box = el('div', 'kpi' + (tone ? ` ${tone}` : ''));
    box.append(el('span', 'kpi-label', name), el('strong', 'kpi-value', value), el('span', 'kpi-sub', sub));
    if (extra) box.append(extra);
    tiles.append(box);
  };
  tile('执行中', data.totals.active, data.totals.waiting_for_user ? `其中 ${data.totals.waiting_for_user} 项等待人工`
    : `共 ${data.totals.chains} 条任务链`);
  tile('近 14 天执行', stats.executions, `日均 ${stats.perDay.toFixed(1)} 次`);
  tile('近 14 天成功率', stats.rate == null ? '—' : `${Math.round(stats.rate * 100)}%`,
    stats.finished ? `${stats.succeeded} / ${stats.finished} 次已结束` : '暂无已结束的执行', sparkline(dailyRates(data.daily)));
  tile('待人工处理', review + rework + waiting, `审核 ${review} · 返工 ${rework} · 等待 ${waiting}`, null,
    review + rework + waiting ? 'attention' : '');
  return tiles;
}

function sparkline(rates) {
  const width = 120, height = 28, chart = svg('svg', {viewBox: `0 0 ${width} ${height}`, class: 'sparkline', 'aria-hidden': 'true'});
  const step = rates.length > 1 ? width / (rates.length - 1) : width;
  let path = '', open = false;
  rates.forEach((rate, i) => {
    if (rate == null) { open = false; return; }
    const x = (i * step).toFixed(1), y = (2 + (1 - rate) * (height - 4)).toFixed(1);
    path += `${open ? 'L' : 'M'}${x} ${y} `; open = true;
  });
  if (path) chart.append(svg('path', {d: path.trim(), class: 'spark-line'}));
  return chart;
}

function dailyChart(data, width) {
  const box = card('每日执行趋势', '按提交日期 · 近 14 天');
  box.append(legend(OUTCOMES));
  const tip = tooltip(box);
  const daily = data.daily || [];
  const W = Math.max(280, Math.round(width || 640)), H = 200, left = 34, right = 8, top = 10, bottom = 24;
  const plotW = W - left - right, plotH = H - top - bottom;
  const scale = niceScale(Math.max(0, ...daily.map(day => OUTCOMES.reduce((t, o) => t + (day[o.key] || 0), 0))));
  const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: 'viz-chart', role: 'img',
    'aria-label': `近 14 天每日执行数，最多 ${scale.max} 次`});
  for (const tick of scale.ticks) {
    const y = top + plotH - tick / scale.max * plotH;
    chart.append(svg('line', {x1: left, x2: W - right, y1: y, y2: y, class: tick ? 'grid' : 'baseline'}));
    const text = svg('text', {x: left - 6, y: y + 3, class: 'axis', 'text-anchor': 'end'}); text.textContent = String(tick);
    chart.append(text);
  }
  const band = plotW / Math.max(1, daily.length), barW = Math.min(28, band * 0.62);
  const every = band < 26 ? 3 : band < 40 ? 2 : 1;
  daily.forEach((day, i) => {
    const x = left + i * band + (band - barW) / 2;
    const segments = stackDay(day, scale.max, plotH);
    segments.forEach((segment, index) => {
      const y = top + segment.y, h = segment.height, last = index === segments.length - 1;
      const r = last ? Math.min(4, h, barW / 2) : 0;
      chart.append(svg('path', {class: 'bar', fill: segment.color,
        d: `M${x} ${y + h}V${y + r}${r ? `Q${x} ${y} ${x + r} ${y}` : ''}H${x + barW - r}${r ? `Q${x + barW} ${y} ${x + barW} ${y + r}` : ''}V${y + h}Z`}));
    });
    if ((daily.length - 1 - i) % every === 0) {
      const text = svg('text', {x: left + i * band + band / 2, y: H - 8, class: 'axis', 'text-anchor': 'middle'});
      text.textContent = dayLabel(day.date); chart.append(text);
    }
    const total = OUTCOMES.reduce((t, o) => t + (day[o.key] || 0), 0);
    const hit = svg('rect', {x: left + i * band, y: top, width: band, height: plotH, class: 'hit', tabindex: 0,
      'aria-label': `${day.date}：共 ${total} 次，` + OUTCOMES.map(o => `${o.label} ${day[o.key] || 0}`).join('，')});
    hover(hit, tip, () => [`${day.date} · 共 ${total} 次`, ...OUTCOMES.map(o => `${o.icon} ${o.label} ${day[o.key] || 0}`)]);
    chart.append(hit);
  });
  const frame = el('div', 'viz-frame'); frame.append(chart); box.append(frame);
  box.append(dataTable('每日执行数', ['日期', ...OUTCOMES.map(o => o.label), '合计'],
    daily.map(day => [day.date, ...OUTCOMES.map(o => day[o.key] || 0), OUTCOMES.reduce((t, o) => t + (day[o.key] || 0), 0)])));
  return box;
}

function agentChart(data, colors) {
  const box = card('执行器对比', '全部历史 · 中位时长为提交到结束');
  box.append(legend(OUTCOMES));
  const tip = tooltip(box);
  const rows = el('div', 'agent-rows');
  const max = Math.max(1, ...data.agents.map(agent => agent.executions));
  for (const agent of data.agents) {
    const other = Math.max(0, agent.executions - agent.succeeded - agent.failed - agent.active);
    const values = {succeeded: agent.succeeded, other, failed: agent.failed, active: agent.active};
    const row = el('div', 'agent-row');
    row.tabIndex = 0;
    const name = el('span', 'agent-name');
    name.append(swatch(colors.get(agent.agent) || 'var(--viz-other)', ''), el('span', '', agent.agent || '未记录'));
    const track = el('span', 'agent-track');
    const bar = el('span', 'agent-bar');
    bar.style.width = `${agent.executions / max * 100}%`;
    for (const outcome of OUTCOMES) {
      if (!values[outcome.key]) continue;
      const part = el('span', 'agent-part');
      part.style.flexGrow = String(values[outcome.key]);
      part.style.background = outcome.color;
      bar.append(part);
    }
    track.append(bar);
    row.append(name, track, el('span', 'agent-meta', `${agent.executions} 次 · 中位 ${formatDuration(agent.median_seconds)}`));
    hover(row, tip, () => [`${agent.agent || '未记录'} · 共 ${agent.executions} 次`,
      ...OUTCOMES.map(o => `${o.icon} ${o.label} ${values[o.key]}`), `中位时长 ${formatDuration(agent.median_seconds)}`]);
    rows.append(row);
  }
  if (!data.agents.length) rows.append(el('p', 'muted', '还没有执行记录。'));
  box.append(rows, dataTable('各执行器执行数', ['执行器', ...OUTCOMES.map(o => o.label), '合计', '中位时长'],
    data.agents.map(agent => [agent.agent || '未记录', agent.succeeded,
      Math.max(0, agent.executions - agent.succeeded - agent.failed - agent.active), agent.failed, agent.active,
      agent.executions, formatDuration(agent.median_seconds)])));
  return box;
}

function workChart(data) {
  const buckets = workBuckets(data.work_status), total = buckets.reduce((t, b) => t + b.count, 0);
  const box = card('任务链状态分布', `共 ${total} 条 · 按当前尝试`);
  const tip = tooltip(box);
  const bar = el('div', 'work-bar');
  bar.setAttribute('role', 'img');
  bar.setAttribute('aria-label', buckets.filter(b => b.count).map(b => `${b.label} ${b.count} 条`).join('，') || '暂无任务链');
  for (const bucket of buckets) {
    if (!bucket.count) continue;
    const part = el('span', 'work-part');
    part.style.flexGrow = String(bucket.count);
    part.style.background = bucket.color;
    hover(part, tip, () => [`${bucket.icon} ${bucket.label}`, `${bucket.count} 条 · ${bucket.percent}%`]);
    bar.append(part);
  }
  if (!total) bar.append(el('span', 'work-empty'));
  const list = el('ul', 'work-legend');
  for (const bucket of buckets) {
    const item = el('li', bucket.count ? '' : 'zero');
    item.append(swatch(bucket.color, bucket.icon), el('span', 'work-label', bucket.label),
      el('span', 'work-count', bucket.count), el('span', 'work-percent', `${bucket.percent}%`));
    list.append(item);
  }
  box.append(bar, list);
  return box;
}

function attentionList(data, onSelect, now) {
  const box = card('需要关注', data.attention_total > data.attention.length
    ? `显示最近 ${data.attention.length} 条，共 ${data.attention_total} 条` : '');
  const list = el('ul', 'attention');
  const buckets = new Map(WORK_BUCKETS.flatMap(bucket => bucket.statuses.map(status => [status, bucket])));
  for (const item of data.attention) {
    const status = ['needs_review', 'rework_required'].includes(item.work_status) ? item.work_status : item.state;
    const bucket = buckets.get(status) || WORK_BUCKETS.at(-1);
    const entry = el('li');
    const button = el('button', 'attention-item');
    button.type = 'button';
    button.dataset.chainId = item.chain_id;
    const badge = el('span', 'attention-badge');
    badge.append(swatch(bucket.color, bucket.icon), el('span', '', label(status)));
    button.append(badge, el('span', 'attention-title', item.title || item.chain_id),
      el('span', 'attention-meta', `${item.agent || '未记录'} · ${relativeTime(item.updated, now)}`));
    button.addEventListener('click', () => onSelect(item.chain_id, item.task_id));
    entry.append(button);
    list.append(entry);
  }
  if (!data.attention.length) {
    const calm = el('p', 'attention-calm');
    calm.append(swatch('var(--viz-ok)', '✓'), el('span', '', '目前没有需要人处理的任务链。'));
    box.append(calm);
  } else box.append(list);
  return box;
}

/** Replace `container` with the overview. `onSelect(chainId, taskId)` opens a chain. */
export function renderOverview(container, data, {onSelect = () => {}, agents = [], now, width} = {}) {
  const colors = agentColors(agents, data.agents.map(agent => agent.agent));
  const grid = el('div', 'viz-grid');
  grid.append(agentChart(data, colors), workChart(data));
  const root = el('div', 'overview');
  root.append(kpis(data), dailyChart(data, width), grid, attentionList(data, onSelect, now ?? Date.now() / 1000));
  container.replaceChildren(root);
  return root;
}
