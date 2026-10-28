import {createAPI, consumeTicket, isUnchanged} from './api.mjs';
import {DashboardState, RequestGate, chainIdle, POLL_ACTIVE, POLL_IDLE} from './state.mjs';
import {renderOverview} from './overview.mjs';
import {agentColors, relativeTime, stateOutcome} from './viz.mjs';
import {failedRecord, renderChainStats, renderGantt} from './chainviz.mjs';
import {createPalette} from './command.mjs';
import {stagger, viewTransition} from './motion.mjs';
import {THEMES, THEME_ICONS, THEME_LABELS, applyTheme, nextTheme, readFlag, readTheme, saveFlag, saveTheme} from './theme.mjs';
import {createGraph} from './graph.mjs';
import {
  TimelineExpansion, timelineRecords, coverageNotes, kindLabel, provenanceLabel, statusLabel,
} from './timeline.mjs';
import {
  names, label, verificationLabel, isRemoteUncertain, stateLabel, workStatusLabel,
} from './status_labels.mjs';

const api = createAPI(), state = new DashboardState(), expansion = new TimelineExpansion();
const $ = id => document.getElementById(id);
const agentLabels = {cursor_cli:'Cursor',cursor:'Cursor',codex:'Codex',claude:'Claude',muse:'Muse',grokbot:'Grok',grok:'Grok'};
const text = value => typeof value === 'string' ? value : value == null ? '未记录' : JSON.stringify(value);
function messageIdentity(record) {
  if (record.actor || record.recipient) {
    return [record.actor, record.recipient].filter(Boolean).join(' → ');
  }
  if (record.title && record.role_missing) return record.title;
  if (record.kind === 'message') {
    if (record.role === 'assistant') return record.agent || '执行器';
    if ({user:'任务输入',system:'系统记录',tool:'工具记录'}[record.role]) {
      return {user:'任务输入',system:'系统记录',tool:'工具记录'}[record.role];
    }
    const known = agentLabels[record.agent];
    if (known) return `${known} · 任务消息`;
    if (record.agent) return `${record.agent} · 任务消息`;
    return record.title || '任务消息';
  }
  return record.agent || kindLabel(record.kind);
}
function node(tag, className, value) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (value != null) item.textContent = text(value);
  return item;
}
function date(value) {
  if (value == null) return '时间未记录';
  const d = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(d.getTime()) ? '时间未记录' : d.toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
let view = 'messages', pageHistory = [], timer, chainTimer, listTimer, searchTimer, stopped = false, listPending = false, agentsPending = false;
let contentKey = '', contentContext = '', renderedEvents = new Map(), listKey = '';
let renderedMessages = new Map();
// Set when a chain's timeline is (re)opened; the first render that has cards plays their entrance.
let pendingEntrance = false;
let historyLoading = false;
// Overview mode: shown while no chain is selected; it polls on its own slow cadence.
const POLL_OVERVIEW = 30000;
const overviewGate = new RequestGate();
let overviewMode = true, overviewTimer, overviewData = null, configuredAgents = [], resizeTimer;
// Execution view inside the graph panel: 'graph' (relations) or 'gantt' (time); kept across chains.
let chainView = 'graph';
// True while the execution poll waits on the idle cadence and can be pulled forward.
let detailIdleWait = false;
// Anchor of the chain response held in state.chain; a 304 for the same anchor repeats it.
let chainAnchor = null;
// Hidden tabs schedule nothing; each skipped loop resumes once the tab is visible.
// Resumed loops re-check their request gate, so stale selections stay inert.
const hidden = () => document.visibilityState === 'hidden';
const resume = new Map();
function later(key, run, delay) {
  if (hidden()) { resume.set(key, run); return null; }
  return setTimeout(run, delay);
}
document.addEventListener('visibilitychange', () => {
  if (hidden() || stopped) return;
  const pending = [...resume.values()]; resume.clear();
  for (const run of pending) run();
});
const graph = createGraph($('graph-viewport'),$('graph-stage'),id=>selectNode(id,true),label);
// These are execution states from the Hub contract; executor names remain dynamic.
const stateFilter = $('state-filter');
stateFilter.replaceChildren(new Option('全部状态',''));
for (const value of ['queued','running','waiting_for_user','succeeded','failed','timed_out','cancelled','interrupted','delivery_unknown']) stateFilter.append(new Option(names[value],value));
/** The top bar's status pill: its dot pulses while live and turns red on failure. */
function connection(text, state) { $('connection').textContent = text; $('connection').dataset.state = state; }
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function failure(error) {
  if (error.status === 401 || error.status === 403) {
    stopped = true; clearTimeout(timer); clearTimeout(chainTimer); clearTimeout(listTimer); clearTimeout(searchTimer); clearTimeout(overviewTimer); overviewGate.invalidate(); state.detailGate.invalidate(); state.chainGate.invalidate(); state.listGate.invalidate();
    connection('会话已失效', 'stopped'); notice(error.message);
  } else connection('连接中断 · 可重试', 'error');
}
function empty(title, description) {
  const wrapper = node('div','empty-state');
  wrapper.append(node('span','empty-symbol','◇'), node('h2','',title), node('p','',description));
  return wrapper;
}
function renderList() {
  // The minute keeps relative times ("3 分钟前") fresh between data changes.
  const key = JSON.stringify([state.items,state.chainId,state.offset,state.nextOffset,pageHistory,configuredAgents,Math.floor(Date.now()/60000)]);
  if (key === listKey) return;
  listKey = key;
  const focusedId = document.activeElement?.dataset?.taskId;
  const scroll = $('task-list').scrollTop;
  const list = $('task-list'); list.replaceChildren();
  for (const task of state.items) {
    const button = node('button', 'task' + (state.chainId === task.id ? ' active' : ''));
    button.dataset.taskId = task.id;
    button.setAttribute('aria-pressed', String(state.chainId === task.id));
    const meta = node('div','task-sub');
    const who = node('span','task-agents');
    const colors = agentColors(configuredAgents, task.agents || []);
    for (const [index, agent] of (task.agents || []).entries()) {
      const dot = node('span','agent-dot'); dot.style.background = colors.get(agent); dot.setAttribute('aria-hidden','true');
      who.append(...(index ? [node('span','',' / ')] : []), dot, node('span','',agent));
    }
    if (!task.agents?.length) who.append(node('span','','执行器未记录'));
    meta.append(who,node('span','',stateLabel(task)));
    if (task.active_count) button.append(node('span','chain-activity',`${task.active_count} 项进行中`));
    button.append(node('div','task-title',task.title || task.id),meta,stateStrip(task));
    button.addEventListener('click',()=>selectChain(task.id)); list.append(button);
    if (focusedId === task.id) button.focus({preventScroll:true});
  }
  if (!state.items.length) list.append(node('p','list-empty','没有匹配的任务链。试试调整筛选条件。'));
  $('list-count').textContent = state.items.length ? `本页 ${state.items.length}` : '0';
  $('page-label').textContent = state.items.length ? `${state.offset + 1}–${state.offset + state.items.length}` : '无记录';
  $('previous').disabled = !pageHistory.length; $('next').disabled = state.nextOffset == null;
  list.scrollTop = scroll;
}
/** One cell per recent execution, coloured by outcome; the accessible name carries the counts. */
function stateStrip(task) {
  const row = node('div','task-strip'), cells = node('span','state-strip');
  const counts = new Map();
  for (const value of task.member_states || []) {
    const outcome = stateOutcome(value);
    const cell = node('span', outcome.key === 'active' ? 'state-cell live' : 'state-cell'); cell.style.background = outcome.color;
    cells.append(cell);
    counts.set(outcome.label, (counts.get(outcome.label) || 0) + 1);
  }
  if (task.member_states_omitted) cells.append(node('span','state-more',`+${task.member_states_omitted}`));
  const summary = [...counts].map(([name, count]) => `${name} ${count}`).join('，');
  cells.setAttribute('role','img');
  cells.setAttribute('aria-label', `最近 ${(task.member_states || []).length} 次执行：${summary || '无记录'}`
    + (task.member_states_omitted ? `；另有 ${task.member_states_omitted} 次更早的执行` : ''));
  const time = node('time','task-time',`${task.task_count} 次${task.updated ? ' · ' + relativeTime(task.updated) : ''}`);
  if (task.updated) time.setAttribute('title', date(task.updated));
  row.append(cells,time);
  return row;
}
function scheduleList() {
  clearTimeout(listTimer);
  if (!stopped) listTimer = later('list',()=>loadList(state.offset,true),5000);
}
async function loadList(offset = 0, background = false) {
  if (stopped) return;
  if (background && listPending) { scheduleList(); return; }
  clearTimeout(listTimer); listPending = true;
  const token = state.listGate.next(); $('task-list').setAttribute('aria-busy','true');
  if (!background) { $('previous').disabled = true; $('next').disabled = true; }
  try {
    const data = await api.chains({...state.filters,limit:30,offset},token.signal);
    if (!state.listGate.current(token)) return;
    if (offset > state.offset) pageHistory.push(state.offset);
    else if (offset < state.offset) pageHistory.pop();
    state.items = data.items; state.offset = offset; state.nextOffset = data.next_offset;
    renderList(); $('previous').disabled = !pageHistory.length; $('next').disabled = state.nextOffset == null;
    notice(); connection('已连接', 'live');
  } catch (error) {
    if (!state.listGate.current(token)) return;
    failure(error); notice(error.message);
    $('previous').disabled = !pageHistory.length; $('next').disabled = state.nextOffset == null;
  } finally {
    if (state.listGate.current(token)) { listPending = false; $('task-list').setAttribute('aria-busy','false'); scheduleList(); }
  }
}
function renderCoverage() {
  const note = $('coverage-note');
  if (!note) return;
  if (view !== 'messages' || !state.chain) { note.hidden = true; note.textContent = ''; return; }
  const lines = coverageNotes(state.chain);
  const warnings = {
    timeline_truncated: '时间线已截断，仅展示已载入记录；全部展开/折叠不作用于未载入历史。',
    messages_truncated: '对话已截断，仅展示最近保留的记录。',
  };
  for (const item of state.chain.warnings || []) if (warnings[item]) lines.push(warnings[item]);
  note.textContent = lines.join(' ');
  note.hidden = !lines.length;
}
function cardBody(record) {
  const body = node('div', 'timeline-body');
  if (record.kind === 'message' || record.text) body.append(node('p', 'bubble', record.text || ''));
  const op = record.operation;
  if (op && typeof op === 'object') {
    const list = node('dl', 'operation-fields');
    const range = op.read_range && typeof op.read_range === 'object'
      ? `${op.read_range.start ?? '?'}–${op.read_range.end ?? '?'}` : null;
    const rows = [
      ['工具', op.tool_name], ['路径', op.path_display || op.path], ['路径范围', op.path_scope],
      ['读取行范围', range],
      ['命令', op.command_display], ['工作目录', op.cwd_display], ['退出码', op.exit_code],
      ['耗时(ms)', op.duration_ms], ['测试汇总', op.test_summary], ['变更摘要', op.change_summary],
    ];
    for (const [label, value] of rows) {
      if (value == null || value === '') continue;
      list.append(node('dt', '', label), node('dd', '', value));
    }
    if (op.stdout_excerpt) list.append(node('dt', '', 'stdout'), node('dd', 'excerpt', op.stdout_excerpt));
    if (op.stderr_excerpt) list.append(node('dt', '', 'stderr'), node('dd', 'excerpt', op.stderr_excerpt));
    if (list.children.length) body.append(list);
  }
  if (record.role_missing) body.append(node('p', 'muted', '历史记录未保存发言角色'));
  if (!body.children.length) body.append(node('p', 'muted', '无更多已记录详情。'));
  return body;
}
function renderTimelineCard(record) {
  const open = expansion.isOpen(state.chainId, record);
  const entry = node('details', 'timeline-card message');
  entry.open = open;
  entry.dataset.messageTask = record.task_id || '';
  entry.dataset.kind = record.kind || '';
  if (record.kind !== 'message' && failedRecord(record)) entry.dataset.failed = 'true';
  entry.dataset.recordId = record.id;
  entry.id = `timeline-${record.id}`;
  entry.setAttribute('aria-expanded', String(open));
  const summary = node('summary', 'timeline-summary');
  const meta = node('div', 'message-meta');
  meta.append(node('strong', '', messageIdentity(record)));
  meta.append(node('span', 'kind-pill', kindLabel(record.kind)));
  if (record.status) meta.append(node('span', 'kind-pill', statusLabel(record.status)));
  meta.append(node('time', '', date(record.timestamp)));
  const preview = node('div', 'timeline-preview', record.preview || record.title || '');
  const source = node('div', 'message-source');
  const execution = node('button', 'message-execution', `${record.agent || '未知执行器'} · ${record.task_id || ''}`);
  execution.setAttribute('aria-label', `查看执行 ${record.task_id} 的详情`);
  execution.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); selectNode(record.task_id, false); });
  source.append(
    execution,
    node('span', '', provenanceLabel(record.provenance)),
    node('span', '', `来源：${record.source || '未知'}`),
    node('span', record.completeness === 'complete' ? '' : 'incomplete',
      ({complete:'完整记录',recorded:'已记录片段',truncated:'已截断',possibly_truncated:'可能已截断',unavailable:'采集不可用'})[record.completeness] || '完整性未知'),
  );
  summary.append(meta, preview, source);
  entry.append(summary, cardBody(record));
  entry.addEventListener('toggle', () => {
    expansion.setOpen(state.chainId, record.id, entry.open);
    entry.setAttribute('aria-expanded', String(entry.open));
  });
  return entry;
}
function renderContent() {
  $('event-controls').hidden = view !== 'events' || !state.selected;
  $('events-older').disabled = historyLoading || !state.events.length || !state.eventsTruncated;
  $('events-latest').disabled = historyLoading || !state.historyMode;
  $('events-note').textContent = state.historyMode ? '正在查看历史页，点击返回最新恢复实时事件。'
    : state.eventsTruncated ? '仅保留最近的事件；更早记录可按页读取。' : '已载入全部已记录事件。';
  const content = $('content'), context = view === 'events' ? `${state.selected}:events` : `${state.chainId}:timeline`;
  const records = view === 'messages' ? timelineRecords(state.chain) : null;
  const key = JSON.stringify([context, view === 'events' ? [state.detail, state.events] : records]);
  if (key === contentKey) { renderCoverage(); return; }
  contentKey = key;
  if (context !== contentContext) { content.replaceChildren(); renderedEvents = new Map(); renderedMessages = new Map(); pendingEntrance = true; }
  contentContext = context;
  content.setAttribute('aria-label', view === 'messages' ? '整条任务链的统一时间线' : '选中执行的事件');
  if (view === 'events') {
    renderCoverage();
    if (!state.detail) { content.replaceChildren(empty('正在读取执行','请稍候…')); return; }
    const kept = new Set(state.events.map(event=>event.seq));
    for (const [seq,entry] of renderedEvents) if (!kept.has(seq)) { entry.remove(); renderedEvents.delete(seq); }
    if (!renderedEvents.size) content.replaceChildren(node('p','event-note',`仅显示选中执行 ${state.selected} 的事件。自定义事件仅展示已记录的脱敏内容。`));
    for (const event of state.events) {
      if (renderedEvents.has(event.seq)) continue;
      const entry=node('details','event'), summary=node('summary','',`#${event.seq} · ${event.kind}`);
      summary.append(node('small','',date(event.timestamp)));
      entry.append(summary,node('pre','',JSON.stringify(event.data,null,2)));content.append(entry);renderedEvents.set(event.seq,entry);
    }
    if (!state.events.length) content.append(empty('暂无执行事件','Hub 尚未记录此执行的事件。'));
    return;
  }
  if (!state.chain) { content.replaceChildren(empty('正在读取任务链','请稍候…')); renderCoverage(); return; }
  const next = new Map(), desired = [];
  for (const record of records) {
    const recordKey = JSON.stringify(record);
    const old = renderedMessages.get(record.id);
    let entry = old?.key === recordKey ? old.entry : null;
    if (!entry) entry = renderTimelineCard(record);
    else {
      entry.open = expansion.isOpen(state.chainId, record);
      entry.setAttribute('aria-expanded', String(entry.open));
    }
    desired.push(entry);
    next.set(record.id, {key: recordKey, entry});
  }
  const wanted = new Set(desired);
  for (const child of [...content.children]) if (!wanted.has(child)) child.remove();
  desired.forEach((entry, index) => { if (content.children[index] !== entry) content.insertBefore(entry, content.children[index] || null); });
  renderedMessages = next;
  if (pendingEntrance && desired.length) { stagger(desired.slice(0, 12)); pendingEntrance = false; }
  if (!desired.length) content.replaceChildren(empty('暂无时间线记录', '这条任务链尚未记录消息或操作。'));
  highlightMessages();
  renderCoverage();
}
function highlightMessages() {
  for (const {entry} of renderedMessages.values()) entry.classList.toggle('selected-execution', entry.dataset.messageTask === state.selected);
}
function renderChain() {
  const task=state.chain.chain;
  $('workspace').textContent=task.workspace_label||'工作空间未记录';
  $('task-title').textContent=task.title||task.id;
  const chainLabel = task.work_item_id ? `工作项 ${task.work_item_id}` : `任务链 ${task.id}`;
  $('subtitle').textContent=`${chainLabel} · ${task.task_count} 次执行 · ${(task.agents||[]).join(' / ')}`;
  $('status-line').replaceChildren(node('span','pill',`最近主执行 · ${stateLabel(task)}`),node('span','pill verification',`验收 · ${verificationLabel(task)}`),node('span','pill',`${task.active_count||0} 项进行中`));
  const warnings={dangling_links:'部分关联任务不存在，无法展示完整关系。',cycles:'记录中存在循环关系，图中保留原始连线。',nodes_truncated:'执行节点过多，仅展示部分节点。',messages_truncated:'对话已截断，仅展示最近保留的记录。',timeline_truncated:'时间线已截断，仅展示已载入记录。'};
  const notes=state.chain.warnings.map(item=>warnings[item]||text(item));
  if(state.chain.truncated&&!notes.length)notes.push('此任务链仅展示部分记录。');
  $('chain-warning').textContent=notes.join(' ');$('chain-warning').hidden=!notes.length;
  $('graph-panel').hidden=false;graph.render(state.chain.nodes,state.chain.edges,state.selected);drawGantt();
  $('chain-stats').hidden=false;renderChainStats($('chain-stats'),state.chain);
  $('graph-count').textContent=`${state.chain.nodes.length} 个节点 · ${state.chain.edges.length} 条关联`;
  renderContent();
}
function renderDetail() {
  const task = state.detail.task;
  const target = $('detail-content'), grid = node('dl','details-grid'); target.replaceChildren(grid);
  for (const [key,value] of [['任务编号',task.id],['执行状态',stateLabel(task)],['验收状态',verificationLabel(task)],['执行器',task.agent],['模式',task.mode],['综合状态',workStatusLabel(task)],['创建时间',date(task.created)],['更新时间',date(task.updated)]]) grid.append(node('dt','',key),node('dd','',value));
  if (task.verification_label === 'not_configured' || (!task.verification_label && task.verification_state === 'not_requested' && !state.detail.criteria?.length)) {
    target.append(node('p', 'muted', '未配置验收规则；执行成功不等于独立验收通过。'));
  } else if (task.verification_label === 'completed_no_extra') {
    target.append(node('p', 'muted', '对话策略显式无需额外验收。'));
  }
  if (task.state === 'interrupted' || task.state === 'delivery_unknown') {
    const banner = node('p', 'muted');
    if (isRemoteUncertain(task)) {
      banner.textContent = '本地观测已中断，远端执行状态未知（可能仍在运行）。这不是“执行失败/已停止”。管理端可用 reconcile_task 沿原任务核对，只读看板不会代为派发。';
    } else {
      banner.textContent = '本地执行已中断或未确认完成。这不是远端投递失败的证明；本地 CLI 任务无需 reconcile_task。';
    }
    target.append(banner);
  }
  target.append(node('h2','','已记录关联'));
  const relations = {continuation:'续接至',continued_from:'续接自',switch_executor:'换执行器至',switched_from:'换执行器自',review:'审核任务',review_of:'审核对象',rework:'返修',development:'开发',linked:'已关联'};
  for (const relation of state.detail.relations) {
    const item = node('div','relation'), button = node('button','',`${relations[relation.kind] || relation.kind} · ${relation.task_id}`);
    button.addEventListener('click',()=>state.chain?.nodes.some(n=>n.id===relation.task_id) ? selectNode(relation.task_id,true) : selectChain(relation.task_id,relation.task_id));
    item.append(button,node('small','',`${relation.agent || '未知执行器'} · ${label(relation.state)}`)); target.append(item);
  }
  if (!state.detail.relations.length) target.append(node('p','muted','暂无已记录的关联。'));
  if (state.detail.acceptance_policy) {
    target.append(node('h2','','验收策略'), node('p','muted',state.detail.acceptance_policy));
  }
  for (const [heading,items] of [['验收条件',state.detail.criteria],['验收检查',state.detail.verification_checks || []],['产物记录',state.detail.artifacts]]) {
    target.append(node('h2','',heading)); const list = node('ul','detail-list');
    for (const item of items) list.append(node('li','',Object.entries(item).map(([key,value])=>`${key}: ${text(value)}`).join('\n')));
    target.append(items.length ? list : node('p','muted', heading === '验收检查' ? '尚无检查结果' : '未记录'));
  }
}
async function poll(token, id) {
  if (stopped || !state.detailGate.current(token)) return;
  detailIdleWait = false;
  let delay = POLL_ACTIVE;
  try {
    const initial = !state.historyMode && state.cursor === 0;
    // Store updates and verification transitions do not always emit events, so the
    // detail is read every tick. Live ticks fetch it together with new events.
    let data = null, detail;
    if (initial) {
      data = await api.eventHistory(id,undefined,token.signal);
      if (!state.detailGate.current(token)) return;
      detail = await api.detail(id,token.signal);
    } else if (state.historyMode) detail = await api.detail(id,token.signal);
    else { detail = await api.detail(id,token.signal,state.cursor); data = detail.live_events; }
    if (!state.detailGate.current(token)) return;
    const detailKey = value => value && [value.task,value.relations,value.criteria,value.artifacts,value.verification_checks,value.acceptance_policy];
    // A revalidated (304) response repeats the last body for this execution.
    const changed = !state.detail || (!isUnchanged(detail) && JSON.stringify(detailKey(state.detail)) !== JSON.stringify(detailKey(detail)));
    if (detail.live_events) { const {live_events: _events, ...rest} = detail; detail = rest; }
    state.detail = detail;
    if (data && !(initial ? state.replaceEvents(token,data,false) : state.commitEvents(token,data))) return;
    if (changed) renderDetail();
    renderContent();
    $('detail-error').hidden = true; connection('已连接', 'live');
    $('sync-time').textContent = `同步于 ${new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`;
    if (chainIdle(state.chain, state.detail)) delay = POLL_IDLE;
    if (!initial && data?.events.length >= 200) delay = 0;
  } catch (error) {
    if (!state.detailGate.current(token)) return;
    failure(error); $('detail-error').textContent = `${error.message} 将自动重试，也可点击刷新。`; $('detail-error').hidden = false; delay = 5000;
  } finally {
    if (!stopped && state.detailGate.current(token)) { detailIdleWait = delay === POLL_IDLE; timer = later('detail',()=>poll(token,id),delay); }
    if (state.detailGate.current(token)) $('content').setAttribute('aria-busy','false');
  }
}
async function loadEventPage(historical) {
  if (stopped || !state.selected || historyLoading) return;
  const id = state.selected, before = historical ? state.events[0]?.seq : undefined;
  if (historical && !before) return;
  clearTimeout(timer);
  const token = state.detailGate.next(); historyLoading = true; renderContent();
  try {
    const data = await api.eventHistory(id,before,token.signal);
    if (!state.replaceEvents(token,data,historical)) return;
    contentKey = ''; renderContent();
  } catch (error) {
    if (state.detailGate.current(token)) { failure(error); notice(error.message); }
  } finally {
    if (state.detailGate.current(token)) { historyLoading = false; renderContent(); await poll(token,id); }
  }
}
$('events-older').addEventListener('click',()=>loadEventPage(true));
$('events-latest').addEventListener('click',()=>loadEventPage(false));
async function pollChain(token,id,desiredNode=null) {
  if(stopped||!state.chainGate.current(token))return;
  let delay=POLL_ACTIVE;
  try {
    // The API pins the requested member inside bounded graphs. Keep the inspected
    // execution as the anchor as new members arrive beyond the node limit.
    const anchor=state.selected||desiredNode||id;
    const data=await api.chain(anchor,token.signal);
    if(!state.chainGate.current(token))return;
    // A click during this request may choose a node outside its capped response.
    // Retry with that newer selection instead of replacing the inspected node.
    if(state.selected&&state.selected!==anchor&&!data.nodes.some(n=>n.id===state.selected)){delay=0;return;}
    if(!(isUnchanged(data)&&state.chain&&chainAnchor===anchor)){
      state.chain=data;state.chainId=data.chain.id;chainAnchor=anchor;
      renderList();renderChain();
    } else if(state.chain.chain.active_count){drawGantt();renderChainStats($('chain-stats'),state.chain);}
    $('chain-error').hidden=true;
    if(!state.selected||!data.nodes.some(n=>n.id===state.selected)) {
      const selected=data.nodes.some(n=>n.id===desiredNode) ? desiredNode : data.nodes.some(n=>n.id===data.chain.latest_task_id) ? data.chain.latest_task_id : data.nodes[0]?.id;
      if(selected)selectNode(selected,false);
    }
    connection('已连接','live');
    if(chainIdle(state.chain,state.detail))delay=POLL_IDLE;
    else if(detailIdleWait&&state.selected&&state.detailGate.token){
      // New work arrived while the execution poll slept on the idle cadence.
      clearTimeout(timer);detailIdleWait=false;
      const detailToken=state.detailGate.token,selected=state.selected;
      timer=later('detail',()=>poll(detailToken,selected),0);
    }
  } catch(error) {
    if(!state.chainGate.current(token))return;
    failure(error);$('chain-error').textContent=`${error.message} 将自动重试，也可点击刷新。`;$('chain-error').hidden=false;delay=5000;
    if(!state.chain){$('graph-panel').hidden=true;$('chain-stats').hidden=true;graph.clear();$('content').replaceChildren(empty('任务链暂时无法读取','请等待重试，或点击刷新。'));contentKey='';}
  } finally {
    if(!stopped&&state.chainGate.current(token))chainTimer=later('chain',()=>pollChain(token,id,desiredNode),delay);
  }
}
async function selectChain(id,desiredNode=null) {
  if(stopped)return;
  let token;
  const apply=()=>{
    setOverviewMode(false);
    clearTimeout(timer);clearTimeout(chainTimer);token=state.selectChain(id);
    historyLoading = false;
    contentKey='';contentContext='';renderedEvents=new Map();renderedMessages=new Map();
    graph.clear();$('graph-panel').hidden=true;$('chain-stats').hidden=true;$('chain-warning').hidden=true;$('chain-error').hidden=true;$('zoom-label').textContent='100%';
    closeDrawers();renderList();$('detail-error').hidden=true;
    $('task-title').textContent='正在读取任务链…';$('subtitle').textContent=id;$('status-line').replaceChildren();
    $('detail-content').replaceChildren(node('p','muted','正在读取…'));renderContent();
  };
  // Leaving the overview cross-fades; the browser runs `apply` a frame later, so wait for it.
  const transition=viewTransition(apply);
  if(transition)await transition.updateCallbackDone.catch(()=>{});
  if(token)await pollChain(token,id,desiredNode);
}
async function selectNode(id,scroll) {
  if(stopped||!state.chain?.nodes.some(n=>n.id===id))return;
  clearTimeout(timer);const token=state.select(id);graph.select(id);highlightMessages();drawGantt();
  historyLoading = false;
  $('detail-content').replaceChildren(node('p','muted','正在读取执行…'));$('detail-error').hidden=true;
  if(scroll){
    setView('messages');
    const firstRecord = timelineRecords(state.chain).find(item => item.task_id === id);
    if (firstRecord) expansion.openTarget(state.chainId, firstRecord.id);
    renderContent();
    const first=[...renderedMessages.values()].find(({entry})=>entry.dataset.messageTask===id);
    first?.entry.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'center'});
  }
  else if(view==='events')renderContent();
  await poll(token,id);
}
function setOverviewMode(on) {
  overviewMode = on;
  $('main').classList.toggle('overview-mode', on);
  $('overview-btn').setAttribute('aria-pressed', String(on));
  if (!on) { clearTimeout(overviewTimer); overviewGate.invalidate(); }
}
function drawOverview() {
  if (!overviewData || !overviewMode) return;
  const host = $('overview'), first = !host.querySelector?.('.overview');
  renderOverview(host, overviewData, {
    agents: configuredAgents,
    // The daily chart is laid out for the card's inner width, so its labels never scale down.
    width: Math.max(280, (host.clientWidth || 700) - 34),
    onSelect: (chainId, taskId) => selectChain(chainId, taskId),
    animate: first,
  });
  // Cards rise in once when the overview first appears, not on every refresh.
  if (first) stagger(host.querySelectorAll?.('.kpi, .viz-card') || []);
}
async function loadOverview() {
  if (stopped || !overviewMode) return;
  clearTimeout(overviewTimer);
  const token = overviewGate.next(), host = $('overview');
  try {
    const data = await api.overview(token.signal);
    if (!overviewGate.current(token) || !overviewMode) return;
    if (!(isUnchanged(data) && overviewData)) { overviewData = data; drawOverview(); }
    host.setAttribute('aria-busy', 'false');
    connection('已连接', 'live');
  } catch (error) {
    if (!overviewGate.current(token)) return;
    failure(error);
    if (!overviewData) host.replaceChildren(empty('全局概况暂时无法读取', `${error.message} 将自动重试，也可点击刷新。`));
  } finally {
    if (!stopped && overviewMode && overviewGate.current(token)) overviewTimer = later('overview', loadOverview, POLL_OVERVIEW);
  }
}
function showOverview() {
  if (stopped) return;
  clearTimeout(timer); clearTimeout(chainTimer); state.chainGate.invalidate(); state.detailGate.invalidate();
  state.chainId = null; state.chain = null; state.selected = null; state.detail = null; state.events = []; chainAnchor = null;
  contentKey = ''; contentContext = ''; renderedEvents = new Map(); renderedMessages = new Map();
  graph.clear(); $('graph-panel').hidden = true; $('chain-stats').hidden = true; $('chain-warning').hidden = true; $('chain-error').hidden = true; $('detail-error').hidden = true;
  $('workspace').textContent = '工作空间'; $('task-title').textContent = '任务与协作，一目了然。';
  $('subtitle').textContent = '近 14 天全局概况 · 选择左侧任务链查看执行关系与完整协作过程。';
  $('status-line').replaceChildren(); $('detail-content').replaceChildren(node('p','muted','选择任务后查看上下文。'));
  closeDrawers(); setOverviewMode(true); renderList(); drawOverview(); loadOverview();
}
function showOverviewAnimated() { if (!stopped) viewTransition(showOverview); }
$('overview-btn').addEventListener('click', showOverviewAnimated);
addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { drawOverview(); drawGantt(); }, 150); });
// The chain the timeline last drew in for; bars only sweep in when this changes.
let ganttShownFor = null;
function drawGantt() {
  if (chainView !== 'gantt' || !state.chain || $('graph-panel').classList.contains('collapsed')) return;
  const animate = ganttShownFor !== state.chainId;
  ganttShownFor = state.chainId;
  renderGantt($('gantt'), state.chain, {agents: configuredAgents, selected: state.selected, animate,
    // Laid out for the panel's inner width (padding 16px each side); it scrolls only below 240px.
    width: Math.max(240, ($('gantt').clientWidth || $('graph-panel').clientWidth || 700) - 32),
    onSelect: id => selectNode(id, true)});
}
function setChainView(next) {
  viewTransition(() => applyChainView(next));
}
function applyChainView(next) {
  chainView = next;
  if (next === 'gantt') ganttShownFor = null;
  const collapsed = $('graph-panel').classList.contains('collapsed');
  $('graph-viewport').hidden = collapsed || next !== 'graph';
  $('gantt').hidden = collapsed || next !== 'gantt';
  $('zoom-controls').hidden = next !== 'graph';
  $('view-graph').setAttribute('aria-pressed', String(next === 'graph'));
  $('view-gantt').setAttribute('aria-pressed', String(next === 'gantt'));
  $('graph-hint').textContent = next === 'graph' ? '点击节点定位对话 · 实线表示续接或切换，虚线表示审查'
    : '按执行器分泳道 · 横条从提交到结束，进行中的执行延伸到现在 · 点击定位对话';
  drawGantt();
}
$('view-graph').addEventListener('click', () => setChainView('graph'));
$('view-gantt').addEventListener('click', () => setChainView('gantt'));
function setView(next) {
  view=next;
  for(const item of document.querySelectorAll('[data-view]')){item.classList.toggle('active',item.dataset.view===view);item.setAttribute('aria-pressed',String(item.dataset.view===view));}
}
$('graph-toggle').addEventListener('click',()=>{
  const collapsed=$('graph-panel').classList.toggle('collapsed');
  $('graph-viewport').hidden=collapsed||chainView!=='graph';$('gantt').hidden=collapsed||chainView!=='gantt';$('graph-hint').hidden=collapsed;
  $('graph-toggle').textContent=collapsed?'展开':'收起';$('graph-toggle').setAttribute('aria-expanded',String(!collapsed));
  if(!collapsed)drawGantt();
});
for(const [id,delta] of [['zoom-out',-.1],['zoom-in',.1]])$(id).addEventListener('click',()=>{$('zoom-label').textContent=`${Math.round(graph.zoom(delta)*100)}%`;});
$('graph-reset').addEventListener('click',()=>{graph.reset();$('zoom-label').textContent='100%';});
let drawerFocus;
// Side panels: on wide screens they collapse in place (the grid track animates to zero); below the
// breakpoints the same buttons open them as drawers. Collapsed states are remembered per viewer.
const PANELS = {
  side: {panel: 'sidebar', button: 'side-toggle', drawerBelow: 650, label: '任务链侧栏'},
  detail: {panel: 'detail', button: 'detail-toggle', drawerBelow: 1080, label: '详情栏'},
};
const collapsed = {side: readFlag('side-collapsed'), detail: readFlag('detail-collapsed')};
function syncPanels() {
  for (const [name, spec] of Object.entries(PANELS)) {
    $('shell').classList.toggle(`${name}-collapsed`, collapsed[name]);
    const drawer = innerWidth <= spec.drawerBelow, open = drawer ? $(spec.panel).classList.contains('show') : !collapsed[name];
    $(spec.button).setAttribute('aria-expanded', String(open));
    $(spec.button).setAttribute('aria-label', `${open ? '收起' : '展开'}${spec.label}`);
    $(spec.button).title = `${open ? '收起' : '展开'}${spec.label}`;
  }
}
function togglePanel(name) {
  const spec = PANELS[name];
  if (innerWidth <= spec.drawerBelow) {
    if ($(spec.panel).classList.contains('show')) closeDrawers(); else openDrawer(spec.panel, $(spec.button));
    return;
  }
  collapsed[name] = !collapsed[name];
  saveFlag(`${name}-collapsed`, collapsed[name]);
  syncPanels();
  // Charts are laid out for their width; redraw once the panel has finished moving.
  clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { drawOverview(); drawGantt(); }, 360);
}
function closeDrawers() {
  $('sidebar').classList.remove('show'); $('detail').classList.remove('show'); $('overlay').hidden = true;
  syncPanels();
  if (drawerFocus) { drawerFocus.focus(); drawerFocus = null; }
}
function openDrawer(id, trigger) {
  closeDrawers(); drawerFocus = trigger;
  $(id).classList.add('show'); $('overlay').hidden = false; syncPanels();
  $(id).querySelector('button,input,select')?.focus();
}
$('side-toggle').addEventListener('click',()=>togglePanel('side'));
$('detail-toggle').addEventListener('click',()=>togglePanel('detail'));
syncPanels();
for (const id of ['overlay','close-sidebar','close-detail']) $(id).addEventListener('click',closeDrawers);
// Appearance: the top-bar button cycles 跟随系统 → 浅色 → 深色; the palette can pick one directly.
let theme = readTheme();
function setTheme(next, remember = true) {
  theme = next; applyTheme(theme); if (remember) saveTheme(theme);
  const button = $('theme-toggle');
  button.textContent = THEME_ICONS[theme];
  button.setAttribute('aria-label', `外观：${THEME_LABELS[theme]}，点击切换`);
  button.title = `外观：${THEME_LABELS[theme]}`;
}
setTheme(theme, false);
$('theme-toggle').addEventListener('click', () => {
  // Decide now, animate later: the transition runs its update a frame late, so quick clicks would race.
  const next = nextTheme(theme); theme = next;
  viewTransition(() => setTheme(next));
});
const chainEntry = task => ({id: task.id, group: '任务链', title: task.title || task.id,
  hint: `${task.task_count} 次 · ${stateLabel(task)}`, run: () => selectChain(task.id)});
const palette = createPalette({
  items: () => [
    {id: 'cmd:overview', group: '导航', title: '回到总览', hint: '全局概况', run: showOverviewAnimated},
    ...(state.chainId ? [
      {id: 'cmd:graph', group: '视图', title: '切换到关系图', hint: '执行关系', run: () => setChainView('graph')},
      {id: 'cmd:gantt', group: '视图', title: '切换到时间轴', hint: '执行甘特图', run: () => setChainView('gantt')},
    ] : []),
    {id: 'cmd:refresh', group: '操作', title: '刷新数据', hint: '重新读取', run: () => $('refresh').click()},
    ...THEMES.map(choice => ({id: `cmd:theme-${choice}`, group: '外观', title: `外观：${THEME_LABELS[choice]}`,
      hint: choice === theme ? '当前' : '', run: () => { theme = choice; viewTransition(() => setTheme(choice)); }})),
    ...Object.entries(PANELS).map(([name, spec]) => ({id: `cmd:panel-${name}`, group: '布局',
      title: `${collapsed[name] ? '展开' : '收起'}${spec.label}`, hint: '', run: () => togglePanel(name)})),
    ...state.items.map(chainEntry),
  ],
  // Chains beyond the loaded sidebar page come from the server's own search.
  search: async query => (await api.chains({q: query.slice(0, 500), limit: 8, offset: 0})).items.map(chainEntry),
});
$('command-key').textContent = /Mac|iP(hone|ad)/.test(globalThis.navigator?.platform || '') ? '⌘K' : 'Ctrl K';
$('command-btn').addEventListener('click', () => palette.open());
document.addEventListener('keydown',event=>{
  if ((event.metaKey || event.ctrlKey) && String(event.key || '').toLowerCase() === 'k') {
    event.preventDefault(); if (palette.isOpen()) palette.close(); else palette.open(); return;
  }
  if (event.key === 'Escape') closeDrawers();
  if (event.key !== 'Tab' || !drawerFocus) return;
  const panel = $('sidebar').classList.contains('show') ? $('sidebar') : $('detail');
  const focusable = [...panel.querySelectorAll('button,input,select')].filter(x=>!x.disabled && x.getClientRects().length);
  const first = focusable[0], last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
});
addEventListener('resize',closeDrawers);
for (const tab of document.querySelectorAll('[data-view]')) tab.addEventListener('click',()=>{
  viewTransition(()=>{ setView(tab.dataset.view); if (state.chainId) renderContent(); });
});
function filtersChanged() {
  clearTimeout(searchTimer); clearTimeout(listTimer); listPending = false;
  state.changeFilters({q:$('search').value.slice(0,500),state:$('state-filter').value,agent:$('agent-filter').value}); pageHistory = [];
  renderList(); searchTimer = setTimeout(()=>loadList(),250);
}
$('search').maxLength = 500; $('search').addEventListener('input',filtersChanged);
$('state-filter').addEventListener('change',filtersChanged); $('agent-filter').addEventListener('change',filtersChanged);
$('next').addEventListener('click',()=>{ if (state.nextOffset != null) loadList(state.nextOffset); });
$('previous').addEventListener('click',()=>{ if (pageHistory.length) loadList(pageHistory.at(-1)); });
async function loadAgents() {
  if (stopped || agentsPending) return;
  agentsPending = true;
  try {
    const agents = await api.agents();
    if (stopped) return;
    const selected = $('agent-filter').value;
    $('agent-filter').replaceChildren(new Option('全部执行器',''));
    for (const agent of agents.items) $('agent-filter').append(new Option(agent.id,agent.id));
    $('agent-filter').value = selected;
    $('agent-count').textContent = `${agents.items.length} 个已配置执行器`;
    configuredAgents = agents.items.map(agent => agent.id);
  } catch (error) { failure(error); notice(error.message); $('agent-count').textContent = '执行器列表不可用 · 可刷新重试'; }
  finally { agentsPending = false; }
}
$('refresh').addEventListener('click',()=>{ if (stopped) { notice('会话已失效，请通过本机 Hub 命令重新打开看板。'); return; } loadAgents(); loadList(state.offset); if (state.chainId) selectChain(state.chainId,state.selected); else loadOverview(); });
function applyBulk(open) {
  if (!state.chainId) return;
  expansion.setAll(state.chainId, open);
  contentKey = '';
  renderContent();
}
$('expand-all')?.addEventListener('click', () => applyBulk(true));
$('collapse-all')?.addEventListener('click', () => applyBulk(false));
async function start() {
  // Ticket stripping happens synchronously inside consumeTicket, before the first await.
  try {
    await consumeTicket(api);
    await loadAgents();
    await loadList();
    loadOverview();
  } catch (error) { failure(error); notice(error.message); $('task-list').setAttribute('aria-busy','false'); $('agent-count').textContent = '执行器列表不可用'; }
}
start();
