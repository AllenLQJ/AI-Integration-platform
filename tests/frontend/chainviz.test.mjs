import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ACTIVITY_KINDS, GANTT, chainStats, failedRecord, ganttLayout, renderChainStats, renderGantt, tickLabel, tickStep, ticks,
} from '../../src/agent_hub/dashboard_static/chainviz.mjs';

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

const T = 1_700_000_000;
const run = (id, agent, created, updated, state = 'succeeded') => ({id, agent, created, updated, state});
const nodes = [
  run('c1', 'claude', T, T + 600),
  run('x1', 'codex', T + 300, T + 1500, 'failed'),
  run('x2', 'codex', T + 900, T + 1200),           // overlaps x1 → second row
  run('x3', 'codex', T + 1800, T + 1800),          // zero length → minimum width
  run('m1', 'muse', T + 2000, T + 2100, 'running'), // still running → extends to now
];

test('lanes follow the configured executor order and overlapping runs take extra rows', () => {
  const layout = ganttLayout(nodes, {now: T + 3600, width: 700, agents: ['codex', 'claude']});
  assert.deepEqual(layout.lanes.map(lane => lane.agent), ['codex', 'claude', 'muse']);
  const codex = layout.lanes[0];
  assert.deepEqual(codex.bars.map(bar => [bar.id, bar.row]), [['x1', 0], ['x2', 1], ['x3', 0]]);
  assert.equal(codex.height, 2 * GANTT.row);
  assert.equal(layout.lanes[1].y, GANTT.axis + 2 * GANTT.row);
  assert.equal(layout.height, GANTT.axis + 4 * GANTT.row + 4);
  // Executions are numbered by creation order, not by lane.
  assert.deepEqual(layout.bars.map(bar => bar.index).sort(), [1, 2, 3, 4, 5]);
});

test('time maps onto the plot, running work reaches now, and tiny runs stay visible', () => {
  const now = T + 3600;
  const layout = ganttLayout(nodes, {now, width: 700, agents: []});
  const bar = id => layout.bars.find(item => item.id === id);
  assert.equal(layout.start, T);
  assert.equal(layout.end, now);
  assert.equal(bar('c1').x, GANTT.label);
  assert.equal(Math.round(bar('m1').x + bar('m1').width), Math.round(layout.x(now)));
  assert.ok(bar('m1').active);
  assert.equal(bar('x3').width, GANTT.minBar);
  assert.equal(Math.round(layout.now), Math.round(GANTT.label + layout.plot));
  // Nothing running: no "now" marker.
  assert.equal(ganttLayout(nodes.slice(0, 4), {now, width: 700}).now, null);
});

test('a single instant still gets a one-minute span', () => {
  const layout = ganttLayout([run('a', 'codex', T, T)], {now: T + 99999, width: 500});
  assert.equal(layout.end - layout.start, 60);
  assert.equal(layout.bars[0].width, GANTT.minBar);
});

test('tick spacing picks minutes, hours or days from the span', () => {
  assert.equal(tickStep(30 * 60, 600), 5 * 60);
  assert.equal(tickStep(6 * 3600, 600), 3600);
  assert.equal(tickStep(2 * 86400, 600), 12 * 3600);
  assert.equal(tickStep(60 * 86400, 600), 14 * 86400);
  // Local clock times, so the expectations hold in every time zone (CI runs in UTC).
  const local = (...parts) => new Date(2026, 9, ...parts).getTime() / 1000;
  const marks = ticks(local(27, 9, 10), local(27, 12, 30), 3600);
  assert.deepEqual(marks, [local(27, 10), local(27, 11), local(27, 12)]);
  assert.equal(tickLabel(marks[0], 3600, null), '10/27 10:00');
  assert.equal(tickLabel(marks[1], 3600, marks[0]), '11:00');
  // Crossing midnight brings the date back.
  assert.equal(tickLabel(local(28, 0), 3600, local(27, 23)), '10/28 00:00');
  assert.equal(tickLabel(local(27, 15), 86400, null), '10/27');
});

test('renders bars that announce themselves, highlight the selection and open an execution', () => {
  const host = new Node('div'), opened = [];
  renderGantt(host, {nodes, warnings: ['nodes_truncated']}, {now: T + 3600, width: 700, agents: ['codex'], selected: 'x2',
    onSelect: id => opened.push(id)});
  const bars = host.all(node => (node.attributes.class || '').startsWith('gantt-bar'));
  assert.equal(bars.length, 5);
  const selected = bars.filter(bar => bar.attributes.class.includes('selected'));
  assert.deepEqual(selected.map(bar => bar.attributes['data-task']), ['x2']);
  const x1 = bars.find(bar => bar.attributes['data-task'] === 'x1');
  assert.equal(x1.attributes.role, 'button');
  assert.equal(x1.attributes.tabindex, '0');
  assert.match(x1.attributes['aria-label'], /第 2 次执行，codex，失败，时长 20 分钟/);
  x1.listeners.click();
  x1.listeners.keydown({key: 'Enter'});
  x1.listeners.keydown({key: 'a'});
  assert.deepEqual(opened, ['x1', 'x1']);
  assert.equal(typeof x1.listeners.pointermove, 'function');
  assert.equal(host.all(node => node.attributes.class === 'now-line').length, 1);
  // No tick label sits on top of the "now" label.
  const nowX = Number(host.all(node => node.attributes.class === 'now-line')[0].attributes.x1);
  const labels = host.all(node => node.attributes.class === 'axis').map(node => Number(node.attributes.x));
  assert.ok(labels.length > 0);
  assert.ok(labels.every(x => Math.abs(x - nowX) >= 40), `tick label within 40px of now: ${labels} vs ${nowX}`);
  assert.match(host.text, /只显示部分执行/);
  const rows = host.byClass('viz-data')[0].all(node => node.tagName === 'tr');
  assert.equal(rows.length, 6);
  assert.match(rows[5].text, /第 5 次muse执行中/);
});

test('an empty chain says so instead of drawing an empty chart', () => {
  const host = new Node('div');
  renderGantt(host, {nodes: [], warnings: []});
  assert.match(host.text, /还没有执行记录/);
  assert.equal(host.all(node => node.tagName === 'svg').length, 0);
});

const entry = (kind, at, extra = {}) => ({id: `${kind}-${at}`, kind, timestamp: T + at, status: 'completed', ...extra});
const timeline = [
  entry('message', 0),
  entry('file_read', 100), entry('file_read', 200),
  entry('file_change', 300),
  entry('command', 400, {operation: {exit_code: 0}}),
  entry('command', 500, {status: 'completed', operation: {exit_code: 2}}),   // non-zero exit counts as failed
  entry('test', 600, {status: 'failed'}),                                      // executor said failed
  entry('test', 700, {operation: {exit_code: 0}}),
  entry('test', 750, {status: 'started'}),                                     // not a finished run
  entry('mystery', 800),                                                       // unknown kinds count as system
  entry('message', 3500, {status: 'failed'}),                                  // messages never count as failures
];

test('failures come from the executor status or a non-zero exit code', () => {
  assert.equal(failedRecord({status: 'failed'}), true);
  assert.equal(failedRecord({status: 'completed', operation: {exit_code: 1}}), true);
  assert.equal(failedRecord({status: 'completed', operation: {exit_code: '0'}}), false);
  assert.equal(failedRecord({status: 'completed', operation: {}}), false);
});

test('chain statistics count executions, records by kind, failures and test runs', () => {
  const stats = chainStats({nodes, timeline, warnings: []}, {now: T + 3600});
  assert.equal(stats.executions, 5);
  assert.deepEqual(stats.outcomes, {succeeded: 3, other: 0, failed: 1, active: 1});
  assert.equal(stats.active, true);
  assert.deepEqual(Object.fromEntries(ACTIVITY_KINDS.map(k => [k.key, stats.kinds[k.key].count])),
    {message: 2, file_read: 2, file_change: 1, command: 2, test: 3, system: 1});
  assert.equal(stats.kinds.command.failed, 1);
  assert.equal(stats.kinds.test.failed, 1);
  assert.equal(stats.kinds.message.failed, 0);
  assert.equal(stats.messages, 2);
  assert.equal(stats.operations, 9);
  assert.equal(stats.operationFailures, 2);
  assert.deepEqual(stats.tests, {runs: 2, failed: 1, passed: 1});
  assert.equal(stats.truncated, false);
});

test('the span reaches now for running work and density splits it into equal slices', () => {
  const stats = chainStats({nodes, timeline, warnings: ['timeline_truncated']}, {now: T + 3600, bins: 6});
  assert.equal(stats.start, T);
  assert.equal(stats.end, T + 3600);
  assert.equal(stats.duration, 3600);
  assert.equal(stats.density.length, 6);
  // 600s slices: 0–500s → first, 600–800s → second, 3500s → last.
  assert.deepEqual(stats.density.map(bin => bin.count), [6, 4, 0, 0, 0, 1]);
  assert.equal(stats.density.reduce((t, bin) => t + bin.count, 0), timeline.length);
  assert.equal(stats.truncated, true);
  // Finished chains stop at their last update.
  assert.equal(chainStats({nodes: nodes.slice(0, 4), timeline: []}, {now: T + 99999}).end, T + 1800);
});

test('a chain without records still produces a zero-length, all-empty summary', () => {
  const stats = chainStats({nodes: [], timeline: []}, {now: T});
  assert.equal(stats.duration, 0);
  assert.equal(stats.density.every(bin => bin.count === 0), true);
  assert.deepEqual(stats.tests, {runs: 0, failed: 0, passed: 0});
});

test('the stats card shows the numbers, flags truncation and offers a table', () => {
  const host = new Node('div');
  renderChainStats(host, {nodes, timeline, warnings: ['timeline_truncated']}, {now: T + 3600});
  const values = host.byClass('stat-value').map(node => node.textContent);
  assert.deepEqual(values, ['1 小时', '5 次', '2', '9', '1 / 2']);
  assert.match(host.text, /仍在进行/);
  assert.match(host.text, /2 次失败/);
  assert.match(host.text, /仅统计已载入的记录/);
  assert.equal(host.byClass('kind-row').length, ACTIVITY_KINDS.length);
  // 24 slices of 150s: records fall into slices 0–5 and 23.
  assert.equal(host.all(node => node.attributes.class === 'density-bar').length, 7);
  assert.equal(host.byClass('viz-data').length, 1);
});
