import {createAPI, consumeTicket} from './api.mjs';
import {DashboardState} from './state.mjs';

const api = createAPI(), state = new DashboardState();
const $ = id => document.getElementById(id);
const names = {queued:'排队中',running:'执行中',waiting_for_user:'等待用户',succeeded:'执行成功',completed:'已完成',failed:'失败',timed_out:'已超时',interrupted:'已中断',delivery_unknown:'投递状态未知',cancelled:'已取消',pending:'待验收',awaiting_verification:'等待验收',verifying:'验收中',rework_required:'需要返工',passed:'验收通过',rejected:'验收未通过',not_requested:'未要求验收',unknown:'未知'};
const label = value => names[value] || value || '未记录';
const text = value => typeof value === 'string' ? value : value == null ? '未记录' : JSON.stringify(value);
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
let view = 'messages', pageHistory = [], timer, listTimer, searchTimer, stopped = false, listPending = false, agentsPending = false;
let contentKey = '', contentContext = '', renderedEvents = new Map(), listKey = '';
let renderedMessages = [], renderedAgent = '';
// These are execution states from the Hub contract; executor names remain dynamic.
const stateFilter = $('state-filter');
stateFilter.replaceChildren(new Option('全部状态',''));
for (const value of ['queued','running','waiting_for_user','succeeded','failed','timed_out','cancelled','interrupted','delivery_unknown']) stateFilter.append(new Option(names[value],value));
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function failure(error) {
  if (error.status === 401 || error.status === 403) {
    stopped = true; clearTimeout(timer); clearTimeout(listTimer); clearTimeout(searchTimer); state.detailGate.invalidate(); state.listGate.invalidate();
    $('connection').textContent = '会话已失效'; notice(error.message);
  } else $('connection').textContent = '连接中断 · 可重试';
}
function empty(title, description) {
  const wrapper = node('div','empty-state');
  wrapper.append(node('span','empty-symbol','◇'), node('h2','',title), node('p','',description));
  return wrapper;
}
function renderList() {
  const key = JSON.stringify([state.items,state.selected,state.offset,state.nextOffset,pageHistory]);
  if (key === listKey) return;
  listKey = key;
  const focusedId = document.activeElement?.dataset?.taskId;
  const scroll = $('task-list').scrollTop;
  const list = $('task-list'); list.replaceChildren();
  for (const task of state.items) {
    const button = node('button', 'task' + (state.selected === task.id ? ' active' : ''));
    button.dataset.taskId = task.id;
    button.setAttribute('aria-pressed', String(state.selected === task.id));
    const meta = node('div','task-sub');
    meta.append(node('span','',task.agent || '执行器未记录'),node('span','',label(task.state)));
    button.append(node('div','task-title',task.title || task.id),meta);
    button.addEventListener('click',()=>select(task.id)); list.append(button);
    if (focusedId === task.id) button.focus({preventScroll:true});
  }
  if (!state.items.length) list.append(node('p','list-empty','没有匹配的任务。试试调整筛选条件。'));
  $('list-count').textContent = state.items.length ? `本页 ${state.items.length}` : '0';
  $('page-label').textContent = state.items.length ? `${state.offset + 1}–${state.offset + state.items.length}` : '无记录';
  $('previous').disabled = !pageHistory.length; $('next').disabled = state.nextOffset == null;
  list.scrollTop = scroll;
}
function scheduleList() {
  clearTimeout(listTimer);
  if (!stopped) listTimer = setTimeout(()=>loadList(state.offset,true),5000);
}
async function loadList(offset = 0, background = false) {
  if (stopped) return;
  if (background && listPending) { scheduleList(); return; }
  clearTimeout(listTimer); listPending = true;
  const token = state.listGate.next(); $('task-list').setAttribute('aria-busy','true');
  if (!background) { $('previous').disabled = true; $('next').disabled = true; }
  try {
    const data = await api.tasks({...state.filters,limit:30,offset},token.signal);
    if (!state.listGate.current(token)) return;
    if (offset > state.offset) pageHistory.push(state.offset);
    else if (offset < state.offset) pageHistory.pop();
    state.items = data.items; state.offset = offset; state.nextOffset = data.next_offset;
    renderList(); $('previous').disabled = !pageHistory.length; $('next').disabled = state.nextOffset == null;
    notice(); $('connection').textContent = '已连接';
  } catch (error) {
    if (!state.listGate.current(token)) return;
    failure(error); notice(error.message);
    $('previous').disabled = !pageHistory.length; $('next').disabled = state.nextOffset == null;
  } finally {
    if (state.listGate.current(token)) { listPending = false; $('task-list').setAttribute('aria-busy','false'); scheduleList(); }
  }
}
function renderContent() {
  const content = $('content');
  const context = `${state.selected}:${view}`;
  const key = JSON.stringify([context,state.detail ? (view === 'events' ? state.events : [state.detail.messages,state.detail.task.agent]) : null]);
  if (key === contentKey) return;
  contentKey = key;
  const sameEvents = context === contentContext && view === 'events' && renderedEvents.size > 0;
  const sameMessages = context === contentContext && view === 'messages' && state.detail && renderedMessages.length > 0 && renderedAgent === state.detail.task.agent && renderedMessages.every((message,index)=>JSON.stringify(message) === JSON.stringify(state.detail.messages[index]));
  if (!sameEvents && !sameMessages) { content.replaceChildren(); renderedEvents = new Map(); renderedMessages = []; }
  contentContext = context;
  content.setAttribute('aria-label', view === 'messages' ? '协作对话' : '执行事件');
  if (!state.detail) { content.append(empty('正在读取任务','请稍候…')); return; }
  if (view === 'events') {
    if (!sameEvents) content.append(node('p','event-note','按事件序号增量读取。自定义事件仅展示已记录的脱敏内容，可能不含完整工具输入输出。'));
    for (const event of state.events) {
      if (renderedEvents.has(event.seq)) continue;
      const entry = node('details','event'), summary = node('summary','',`#${event.seq} · ${event.kind}`);
      summary.append(node('small','',date(event.timestamp)));
      entry.append(summary,node('pre','',JSON.stringify(event.data,null,2))); content.append(entry);
      renderedEvents.set(event.seq,entry);
    }
    if (!state.events.length) content.append(empty('暂无执行事件','Hub 尚未记录此任务的事件。'));
    return;
  }
  const roles = {user:'任务输入',assistant:state.detail.task.agent || '执行器',system:'系统记录',tool:'工具记录',unknown:'来源身份未记录'};
  const completeness = {complete:'完整记录',recorded:'已记录片段',truncated:'已截断',possibly_truncated:'可能已截断'};
  for (const message of state.detail.messages.slice(sameMessages ? renderedMessages.length : 0)) {
    const entry = node('article','message'), body = node('div');
    const meta = node('div','message-meta'), time = node('time','',date(message.timestamp));
    meta.append(node('strong','',roles[message.role] || roles.unknown),time);
    const source = node('div','message-source');
    source.append(node('span','',`来源：${message.source || '未知'}`),node('span',message.completeness === 'complete' ? '' : 'incomplete',completeness[message.completeness] || '完整性未知'));
    body.append(meta,node('p','bubble',message.text),source);
    entry.append(node('div','avatar' + (message.role === 'assistant' ? ' assistant' : ''),message.role === 'user' ? '入' : message.role === 'assistant' ? '答' : '记'),body);
    content.append(entry);
  }
  renderedMessages = state.detail.messages.slice(); renderedAgent = state.detail.task.agent;
  if (!state.detail.messages.length) content.append(empty('暂无对话记录','此任务尚未记录消息。'));
}
function renderDetail() {
  const task = state.detail.task;
  $('workspace').textContent = task.workspace_label || '工作空间未记录';
  $('task-title').textContent = task.title || task.id;
  $('subtitle').textContent = `${task.agent || '执行器未记录'} · ${task.mode || '模式未记录'} · ${task.id}`;
  $('status-line').replaceChildren(node('span','pill',`执行 · ${label(task.state)}`),node('span','pill verification',`验收 · ${label(task.verification_state)}`));
  const target = $('detail-content'), grid = node('dl','details-grid'); target.replaceChildren(grid);
  for (const [key,value] of [['执行器',task.agent],['模式',task.mode],['综合状态',label(task.work_status)],['创建时间',date(task.created)],['更新时间',date(task.updated)]]) grid.append(node('dt','',key),node('dd','',value));
  target.append(node('h2','','已记录关联'));
  const relations = {continuation:'续接至',continued_from:'续接自',switch_executor:'换执行器至',switched_from:'换执行器自',review:'审核任务',review_of:'审核对象'};
  for (const relation of state.detail.relations) {
    const item = node('div','relation'), button = node('button','',`${relations[relation.kind] || relation.kind} · ${relation.task_id}`);
    button.addEventListener('click',()=>select(relation.task_id));
    item.append(button,node('small','',`${relation.agent || '未知执行器'} · ${label(relation.state)}`)); target.append(item);
  }
  if (!state.detail.relations.length) target.append(node('p','muted','暂无已记录的关联。'));
  for (const [heading,items] of [['验收条件',state.detail.criteria],['产物记录',state.detail.artifacts]]) {
    target.append(node('h2','',heading)); const list = node('ul','detail-list');
    for (const item of items) list.append(node('li','',Object.entries(item).map(([key,value])=>`${key}: ${text(value)}`).join('\n')));
    target.append(items.length ? list : node('p','muted','未记录'));
  }
}
async function poll(token, id) {
  if (stopped || !state.detailGate.current(token)) return;
  let delay = 2500;
  try {
    const data = await api.events(id,state.cursor,token.signal);
    if (!state.detailGate.current(token)) return;
    // Store updates and verification transitions do not always emit events.
    const detail = await api.detail(id,token.signal);
    if (!state.detailGate.current(token)) return;
    const changed = JSON.stringify(state.detail && [state.detail.task,state.detail.relations,state.detail.criteria,state.detail.artifacts]) !== JSON.stringify([detail.task,detail.relations,detail.criteria,detail.artifacts]);
    state.detail = detail;
    if (!state.commitEvents(token,data)) return;
    if (changed) renderDetail();
    renderContent();
    $('detail-error').hidden = true; $('connection').textContent = '已连接';
    $('sync-time').textContent = `同步于 ${new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`;
    if (data.events.length >= 200) delay = 0;
  } catch (error) {
    if (!state.detailGate.current(token)) return;
    failure(error); $('detail-error').textContent = `${error.message} 将自动重试，也可点击刷新。`; $('detail-error').hidden = false; delay = 5000;
  } finally {
    if (!stopped && state.detailGate.current(token)) timer = setTimeout(()=>poll(token,id),delay);
    if (state.detailGate.current(token)) $('content').setAttribute('aria-busy','false');
  }
}
async function select(id) {
  if (stopped) return;
  clearTimeout(timer); const token = state.select(id);
  contentKey = ''; contentContext = ''; renderedEvents = new Map(); renderedMessages = [];
  closeDrawers(); renderList(); $('detail-error').hidden = true;
  $('task-title').textContent = '正在读取任务…'; $('subtitle').textContent = id;
  $('status-line').replaceChildren(); $('detail-content').replaceChildren(node('p','muted','正在读取…'));
  $('content').setAttribute('aria-busy','true'); renderContent();
  await poll(token,id);
}
let drawerFocus;
function closeDrawers() {
  $('sidebar').classList.remove('show'); $('detail').classList.remove('show'); $('overlay').hidden = true;
  $('menu-toggle').setAttribute('aria-expanded','false');
  $('detail-toggle').setAttribute('aria-expanded',String(!$('detail').classList.contains('hidden') && innerWidth > 1080));
  if (drawerFocus) { drawerFocus.focus(); drawerFocus = null; }
}
function openDrawer(id, trigger) {
  closeDrawers(); drawerFocus = trigger;
  $(id).classList.add('show'); $('overlay').hidden = false; trigger.setAttribute('aria-expanded','true');
  $(id).querySelector('button,input,select')?.focus();
}
$('menu-toggle').addEventListener('click',()=>openDrawer('sidebar',$('menu-toggle')));
$('detail-toggle').addEventListener('click',()=>{
  if (innerWidth <= 1080) { if ($('detail').classList.contains('show')) closeDrawers(); else openDrawer('detail',$('detail-toggle')); }
  else { const hidden = $('detail').classList.toggle('hidden'); $('shell').classList.toggle('no-detail',hidden); $('detail-toggle').setAttribute('aria-expanded',String(!hidden)); }
});
for (const id of ['overlay','close-sidebar','close-detail']) $(id).addEventListener('click',closeDrawers);
document.addEventListener('keydown',event=>{
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
  view = tab.dataset.view;
  for (const item of document.querySelectorAll('[data-view]')) { item.classList.toggle('active',item === tab); item.setAttribute('aria-pressed',String(item === tab)); }
  if (state.selected) renderContent();
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
  } catch (error) { failure(error); notice(error.message); $('agent-count').textContent = '执行器列表不可用 · 可刷新重试'; }
  finally { agentsPending = false; }
}
$('refresh').addEventListener('click',()=>{ if (stopped) { notice('会话已失效，请通过本机 Hub 命令重新打开看板。'); return; } loadAgents(); loadList(state.offset); if (state.selected) select(state.selected); });
async function start() {
  // Ticket stripping happens synchronously inside consumeTicket, before the first await.
  try {
    await consumeTicket(api);
    await loadAgents();
    await loadList();
  } catch (error) { failure(error); notice(error.message); $('task-list').setAttribute('aria-busy','false'); $('agent-count').textContent = '执行器列表不可用'; }
}
start();
