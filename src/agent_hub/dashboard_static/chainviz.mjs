// Chain views built from the chain detail response: an execution Gantt chart.
// Pure layout functions are exported for tests; rendering uses the shared viz.mjs primitives.
import {label} from './status_labels.mjs';
import {
  OUTCOMES, agentColors, dataTable, el, formatDuration, hover, legend, stateOutcome, svg, tooltip,
} from './viz.mjs';

const TERMINAL = new Set(['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted', 'delivery_unknown']);
const MINUTE = 60, HOUR = 3600, DAY = 86400;
const STEPS = [MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY];

export const GANTT = {label: 96, right: 14, axis: 24, row: 26, bar: 16, minBar: 4, tickSpacing: 84};

/** The smallest step that keeps ticks at least `spacing` pixels apart. */
export function tickStep(span, plotWidth, spacing = GANTT.tickSpacing) {
  const room = Math.max(1, Math.floor(plotWidth / spacing));
  return STEPS.find(step => span / step <= room) ?? STEPS.at(-1);
}

/** Tick times on local clock boundaries (whole hours, midnights...). */
export function ticks(start, end, step) {
  const offset = new Date(start * 1000).getTimezoneOffset() * 60;
  const first = Math.ceil((start - offset) / step) * step + offset;
  const out = [];
  for (let t = first; t <= end; t += step) out.push(t);
  return out;
}

export function tickLabel(t, step, previous) {
  const d = new Date(t * 1000);
  const day = `${d.getMonth() + 1}/${d.getDate()}`;
  if (step >= DAY) return day;
  const clock = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const changed = previous == null || new Date(previous * 1000).toDateString() !== d.toDateString();
  return changed ? `${day} ${clock}` : clock;
}

/**
 * Lay out executions on per-executor lanes. Overlapping runs of one executor take extra rows.
 * Running executions extend to `now`. Returns pixel geometry for a chart `width` wide.
 */
export function ganttLayout(nodes, {now = Date.now() / 1000, width = 640, agents = []} = {}) {
  const runs = [...nodes].sort((a, b) => a.created - b.created || String(a.id).localeCompare(String(b.id)))
    .map((node, index) => {
      const active = !TERMINAL.has(node.state);
      const end = active ? Math.max(now, node.updated ?? node.created) : Math.max(node.updated ?? node.created, node.created);
      return {...node, index: index + 1, start: node.created, end, active, agent: node.agent || '未记录'};
    });
  const order = [...agentColors(agents, runs.map(run => run.agent)).keys()].filter(name => runs.some(run => run.agent === name));
  const start = runs.length ? Math.min(...runs.map(run => run.start)) : now;
  let end = runs.length ? Math.max(...runs.map(run => run.end)) : now;
  if (end - start < MINUTE) end = start + MINUTE;
  const plot = Math.max(120, width - GANTT.label - GANTT.right);
  const x = t => GANTT.label + (t - start) / (end - start) * plot;
  const lanes = [];
  let top = GANTT.axis;
  for (const agent of order) {
    const rowsEnd = [];
    const bars = [];
    for (const run of runs.filter(item => item.agent === agent)) {
      let row = rowsEnd.findIndex(last => last <= run.start);
      if (row < 0) { row = rowsEnd.length; rowsEnd.push(0); }
      rowsEnd[row] = run.end;
      const left = x(run.start);
      bars.push({...run, row, x: left, width: Math.max(GANTT.minBar, x(run.end) - left),
        y: top + row * GANTT.row + (GANTT.row - GANTT.bar) / 2});
    }
    const rows = Math.max(1, rowsEnd.length);
    lanes.push({agent, y: top, height: rows * GANTT.row, bars});
    top += rows * GANTT.row;
  }
  const step = tickStep(end - start, plot);
  return {start, end, width: GANTT.label + plot + GANTT.right, height: top + 4, plot, x,
    lanes, bars: lanes.flatMap(lane => lane.bars), step, ticks: ticks(start, end, step),
    now: runs.some(run => run.active) && now >= start && now <= end ? x(now) : null};
}

function stamp(t) {
  const d = new Date(t * 1000);
  return d.toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}

/** Render the Gantt chart into `host`. `onSelect(id)` focuses an execution. */
export function renderGantt(host, chain, {agents = [], selected = null, now, width, onSelect = () => {}} = {}) {
  const nodes = chain?.nodes || [];
  const box = el('div', 'gantt');
  if (!nodes.length) { box.append(el('p', 'muted', '这条任务链还没有执行记录。')); host.replaceChildren(box); return box; }
  const layout = ganttLayout(nodes, {now, width, agents});
  const colors = agentColors(agents, layout.lanes.map(lane => lane.agent));
  box.append(legend(OUTCOMES));
  const tip = tooltip(box);
  const chart = svg('svg', {viewBox: `0 0 ${layout.width} ${layout.height}`, width: layout.width, height: layout.height,
    class: 'gantt-chart', role: 'group', 'aria-label': `${nodes.length} 次执行的时间轴`});
  let previous = null;
  for (const t of layout.ticks) {
    const x = layout.x(t);
    chart.append(svg('line', {x1: x, x2: x, y1: GANTT.axis - 4, y2: layout.height, class: 'grid'}));
    // The "now" label owns the axis next to its marker.
    if (layout.now != null && Math.abs(x - layout.now) < 40) continue;
    const text = svg('text', {x, y: 12, class: 'axis', 'text-anchor': 'middle'});
    text.textContent = tickLabel(t, layout.step, previous); previous = t;
    chart.append(text);
  }
  for (const lane of layout.lanes) {
    chart.append(svg('line', {x1: 0, x2: layout.width, y1: lane.y + lane.height, y2: lane.y + lane.height, class: 'lane-line'}));
    chart.append(svg('circle', {cx: 8, cy: lane.y + GANTT.row / 2, r: 4, fill: colors.get(lane.agent)}));
    const name = svg('text', {x: 18, y: lane.y + GANTT.row / 2 + 4, class: 'lane-name'});
    name.textContent = lane.agent.length > 11 ? lane.agent.slice(0, 10) + '…' : lane.agent;
    chart.append(name);
  }
  if (layout.now != null) {
    chart.append(svg('line', {x1: layout.now, x2: layout.now, y1: GANTT.axis - 4, y2: layout.height, class: 'now-line'}));
    const text = svg('text', {x: layout.now - 3, y: GANTT.axis - 6, class: 'now-label', 'text-anchor': 'end'});
    text.textContent = '现在'; chart.append(text);
  }
  for (const bar of layout.bars) {
    const outcome = stateOutcome(bar.state);
    const lines = () => [`第 ${bar.index} 次 · ${bar.agent}`, `${outcome.icon} ${label(bar.state)}`,
      `开始 ${stamp(bar.start)}`, bar.active ? '仍在进行' : `结束 ${stamp(bar.end)}`, `时长 ${formatDuration(bar.end - bar.start)}`];
    const group = svg('g', {class: 'gantt-bar' + (bar.id === selected ? ' selected' : ''), tabindex: 0, role: 'button',
      'aria-label': `第 ${bar.index} 次执行，${bar.agent}，${label(bar.state)}，时长 ${formatDuration(bar.end - bar.start)}`,
      'data-task': bar.id});
    group.append(svg('rect', {x: bar.x, y: bar.y - 4, width: bar.width, height: GANTT.bar + 8, class: 'hit'}));
    group.append(svg('rect', {x: bar.x, y: bar.y, width: bar.width, height: GANTT.bar, rx: Math.min(4, bar.width / 2),
      fill: outcome.color, class: 'bar' + (bar.active ? ' active' : '')}));
    if (bar.width > 44) {
      const text = svg('text', {x: bar.x + 6, y: bar.y + GANTT.bar - 4, class: 'bar-label'});
      text.textContent = `#${bar.index}`; group.append(text);
    }
    hover(group, tip, lines);
    group.addEventListener('click', () => onSelect(bar.id));
    group.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault?.(); onSelect(bar.id); } });
    chart.append(group);
  }
  const frame = el('div', 'gantt-frame'); frame.append(chart); box.append(frame);
  if ((chain.warnings || []).includes('nodes_truncated')) box.append(el('p', 'gantt-note', '执行过多，时间轴只显示部分执行。'));
  box.append(dataTable('执行时间轴', ['次序', '执行器', '状态', '开始', '结束', '时长'],
    layout.bars.slice().sort((a, b) => a.index - b.index).map(bar => [`第 ${bar.index} 次`, bar.agent, label(bar.state),
      stamp(bar.start), bar.active ? '进行中' : stamp(bar.end), formatDuration(bar.end - bar.start)])));
  host.replaceChildren(box);
  return box;
}
