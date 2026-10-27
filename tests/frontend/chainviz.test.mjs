import test from 'node:test';
import assert from 'node:assert/strict';
import {GANTT, ganttLayout, tickStep, ticks, tickLabel, renderGantt} from '../../src/agent_hub/dashboard_static/chainviz.mjs';

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
  const marks = ticks(T + 1, T + 3 * 3600, 3600);
  assert.equal(marks.length, 3);
  assert.ok(marks.every((t, i) => i === 0 || t - marks[i - 1] === 3600));
  assert.match(tickLabel(marks[0], 3600, null), /^\d+\/\d+ \d\d:\d\d$/);
  assert.match(tickLabel(marks[1], 3600, marks[0]), /^\d\d:\d\d$/);
  assert.match(tickLabel(T, 86400, null), /^\d+\/\d+$/);
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
