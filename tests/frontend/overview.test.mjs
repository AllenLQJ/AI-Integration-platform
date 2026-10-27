import test from 'node:test';
import assert from 'node:assert/strict';
import {
  OUTCOMES, WORK_BUCKETS, niceScale, workBuckets, windowStats, dailyRates, formatDuration, relativeTime,
  dayLabel, agentColors, stackDay, renderOverview,
} from '../../src/agent_hub/dashboard_static/overview.mjs';

// Just enough DOM for the renderer: elements, SVG elements, attributes and listeners.
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.style = {}; this.dataset = {}; this.textContent = ''; this.hidden = false; this.className = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(kind, fn) { this.listeners[kind] = fn; }
  all(predicate, found = []) { for (const child of this.children) { if (predicate(child)) found.push(child); child.all(predicate, found); } return found; }
  byClass(name) { return this.all(node => (node.className || node.attributes.class || '').split(' ').includes(name)); }
  get text() { return this.textContent + this.children.map(child => child.text).join(''); }
}
globalThis.document = {createElement: tag => new Node(tag), createElementNS: (_ns, tag) => new Node(tag)};

const day = (date, counts = {}) => ({date, succeeded: 0, failed: 0, other: 0, active: 0, ...counts});
const daily = Array.from({length: 14}, (_, i) => day(`2026-10-${String(14 + i).padStart(2, '0')}`));
daily[13] = day('2026-10-27', {succeeded: 3, failed: 1, active: 2});
daily[12] = day('2026-10-26', {succeeded: 1, other: 1});
const overview = {
  totals: {chains: 5, executions: 9, active: 2, waiting_for_user: 1},
  states: {succeeded: 4, failed: 1, running: 2},
  work_status: {completed: 2, needs_review: 1, rework_required: 1, running: 1, weird_new_state: 1},
  agents: [{agent: 'claude', executions: 4, succeeded: 2, failed: 1, active: 1, median_seconds: 95},
           {agent: 'codex', executions: 5, succeeded: 2, failed: 0, active: 1, median_seconds: null}],
  daily,
  attention: [{chain_id: 'wi-1', task_id: 't-1', title: '修复登录', agent: 'codex', state: 'succeeded', work_status: 'needs_review', updated: 1000}],
  attention_total: 3,
};

test('y scale snaps to a 1/2/5 step and never collapses to zero', () => {
  assert.deepEqual(niceScale(0), {max: 1, ticks: [0, 1]});
  assert.deepEqual(niceScale(3), {max: 3, ticks: [0, 1, 2, 3]});
  assert.deepEqual(niceScale(7), {max: 8, ticks: [0, 2, 4, 6, 8]});
  assert.deepEqual(niceScale(37), {max: 40, ticks: [0, 10, 20, 30, 40]});
  assert.equal(niceScale(101).max, 150);
});

test('work statuses fold into fixed buckets whose percents sum to 100', () => {
  const buckets = workBuckets(overview.work_status);
  assert.deepEqual(buckets.map(b => b.key), WORK_BUCKETS.map(b => b.key));
  const count = Object.fromEntries(buckets.map(b => [b.key, b.count]));
  assert.deepEqual(count, {done: 2, plain: 0, person: 2, failed: 0, active: 1, other: 1});
  assert.equal(buckets.reduce((t, b) => t + b.percent, 0), 100);
  assert.equal(buckets.find(b => b.key === 'plain').percent, 0);
  assert.deepEqual(workBuckets({}).map(b => b.percent), WORK_BUCKETS.map(() => 0));
});

test('window totals, success rate over finished runs, and daily gaps', () => {
  const stats = windowStats(daily);
  assert.equal(stats.executions, 8);
  assert.equal(stats.finished, 6);
  assert.equal(stats.rate, 4 / 6);
  const rates = dailyRates(daily);
  assert.equal(rates[0], null);
  assert.equal(rates[13], 3 / 4);
  assert.equal(windowStats([]).rate, null);
});

test('stacked segments keep the outcome order, fill the bar and leave a gap between fills', () => {
  const segments = stackDay(daily[13], 8, 160);
  assert.deepEqual(segments.map(s => s.key), ['succeeded', 'failed', 'active']);
  assert.equal(segments[0].y + segments[0].height, 160);
  assert.equal(segments[1].y + segments[1].height, segments[0].y - 2);
  assert.equal(Math.round(segments.at(-1).y), 160 - 6 / 8 * 160);
  // Success and failure are never stacked next to each other when "other" is present.
  const order = OUTCOMES.map(o => o.key);
  assert.equal(Math.abs(order.indexOf('succeeded') - order.indexOf('failed')) > 1, true);
});

test('formatting', () => {
  assert.equal(formatDuration(null), '—');
  assert.equal(formatDuration(42), '42 秒');
  assert.equal(formatDuration(95), '2 分钟');
  assert.equal(formatDuration(3 * 3600 + 5 * 60), '3 小时 5 分');
  assert.equal(formatDuration(26 * 3600), '1 天 2 小时');
  assert.equal(relativeTime(1000, 1030), '刚刚');
  assert.equal(relativeTime(1000, 1000 + 5 * 60), '5 分钟前');
  assert.equal(relativeTime(1000, 1000 + 3 * 86400), '3 天前');
  assert.equal(dayLabel('2026-10-07'), '10/7');
});

test('executor colours follow the executor, not its rank', () => {
  const colors = agentColors(['codex', 'claude'], ['claude', 'muse']);
  assert.equal(colors.get('codex'), 'var(--viz-agent-1)');
  assert.equal(colors.get('claude'), 'var(--viz-agent-2)');
  assert.equal(colors.get('muse'), 'var(--viz-agent-3)');
  const many = agentColors(Array.from({length: 10}, (_, i) => `a${i}`));
  assert.equal(many.get('a8'), 'var(--viz-other)');
});

test('renders tiles, charts with hover and data tables, and opens a chain from the attention list', () => {
  const host = new Node('section'), opened = [];
  renderOverview(host, overview, {agents: ['codex', 'claude'], now: 1000 + 120, width: 640,
    onSelect: (chain, task) => opened.push([chain, task])});
  const values = host.byClass('kpi-value').map(node => node.textContent);
  assert.deepEqual(values, ['2', '8', '67%', '2']);
  assert.match(host.byClass('kpi-sub')[0].textContent, /1 项等待人工/);
  // The alert tile must not reuse the attention list's class, whose layout rules would leak onto it.
  assert.deepEqual(host.byClass('kpi').map(tile => tile.className), ['kpi', 'kpi', 'kpi', 'kpi alert']);
  // The sparkline stretches to the tile instead of keeping its 120x28 aspect ratio.
  assert.equal(host.byClass('sparkline')[0].attributes.preserveAspectRatio, 'none');
  // Review states read as Chinese labels, never as raw status keys.
  assert.doesNotMatch(host.byClass('attention-item')[0].text, /needs_review/);
  // One hit target per day, each with an accessible summary and a hover tooltip.
  const hits = host.all(node => node.attributes.class === 'hit');
  assert.equal(hits.length, 14);
  assert.match(hits[13].attributes['aria-label'], /2026-10-27：共 6 次/);
  assert.equal(typeof hits[13].listeners.pointermove, 'function');
  // Every chart has a table view; legends name every outcome with an icon.
  assert.equal(host.byClass('viz-data').length, 2);
  assert.ok(OUTCOMES.every(o => host.byClass('viz-legend')[0].text.includes(o.label)));
  // Executor rows keep their configured colour even though the data is ordered by name.
  const swatches = host.byClass('agent-name').map(row => row.children[0].style.background);
  assert.deepEqual(swatches, ['var(--viz-agent-2)', 'var(--viz-agent-1)']);
  assert.match(host.text, /中位 2 分钟/);
  // Attention: label, relative time, the "more" note, and a click opens the chain.
  const item = host.byClass('attention-item')[0];
  assert.match(item.text, /修复登录/);
  assert.match(item.text, /2 分钟前/);
  assert.match(host.text, /显示最近 1 条，共 3 条/);
  item.listeners.click();
  assert.deepEqual(opened, [['wi-1', 't-1']]);
});

test('an empty hub renders without charts breaking', () => {
  const host = new Node('section');
  renderOverview(host, {totals: {chains: 0, executions: 0, active: 0, waiting_for_user: 0}, states: {}, work_status: {},
    agents: [], daily: daily.map(d => day(d.date)), attention: [], attention_total: 0}, {width: 300});
  assert.deepEqual(host.byClass('kpi-value').map(node => node.textContent), ['0', '0', '—', '0']);
  assert.match(host.text, /目前没有需要人处理的任务链/);
  assert.match(host.text, /还没有执行记录/);
});
