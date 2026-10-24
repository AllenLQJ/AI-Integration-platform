import test from 'node:test';
import assert from 'node:assert/strict';

// Minimal DOM surface exercises the real application without a browser dependency.
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.hidden = false; this.dataset = {}; this.classes = new Set(); this.classList = {add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),contains:x=>this.classes.has(x),toggle:(x,force)=>{ const next = force ?? !this.classes.has(x); next ? this.classes.add(x) : this.classes.delete(x); return next; }}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key,value) { this.attributes[key] = value; }
  addEventListener(key,fn) { this.listeners[key] = fn; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() { document.activeElement = this; }
}
const flush = () => new Promise(resolve=>setImmediate(resolve));
test('real app safely renders and recovers initialization while polling preserves interaction and finds no-event changes', async () => {
  const nodes = new Map(), timers = [];
  const original = Object.fromEntries(['document','window','history','location','fetch','Option','addEventListener','innerWidth','setTimeout','clearTimeout'].map(key=>[key,globalThis[key]]));
  const payload = '<img src=x onerror=alert(1)>';
  const task = {id:'task-1',title:payload,agent:'custom-executor',state:'succeeded',verification_state:'awaiting_verification',workspace_label:'workspace',created:1,updated:2,mode:'custom'};
  const requests = [];
  let agentCalls = 0, taskItems = [task], taskState = 'succeeded', eventItems = [{seq:1,kind:'custom',data:{text:payload},timestamp:2}];
  const tabs = ['messages','events'].map(value=>{ const item = new Element('button'); item.dataset.view = value; return item; });
  async function settle() { for (let i=0;i<8;i++) await flush(); }
  async function tick(delay) {
    const scheduled = timers.findLast(item=>item.delay === delay && !item.cancelled && !item.used);
    assert.ok(scheduled,`timer ${delay} scheduled`); scheduled.used = true;
    await scheduled.fn(); await settle();
  }
  try {
    globalThis.document = {getElementById:id=>{ if (!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id); },createElement:tag=>new Element(tag),querySelectorAll:()=>tabs,addEventListener:()=>{}};
    globalThis.window = {location:{hash:'#ticket=one-use',pathname:'/dashboard/',search:''},history:{replaceState:()=>requests.push('strip')}};
    globalThis.Option = class extends Element { constructor(title,value) { super('option'); this.textContent = title; this.value = value; } };
    globalThis.addEventListener = ()=>{}; globalThis.innerWidth = 1200;
    globalThis.setTimeout = (fn,delay)=>{ const item = {fn,delay}; timers.push(item); return item; };
    globalThis.clearTimeout = timer=>{ if (timer) timer.cancelled = true; };
    globalThis.fetch = async url=>{
      requests.push(url);
      if (url.endsWith('/agents') && ++agentCalls === 1) throw new Error('temporary unavailable');
      const data = url.endsWith('/session') ? {} : url.endsWith('/agents') ? {items:[{id:'custom-executor',modes:['custom']}]} : url.includes('/events?') ? {events:eventItems,next_seq:eventItems.at(-1)?.seq || 1} : url.includes('/tasks?') ? {items:taskItems,next_offset:null} : {task:{...task,state:taskState},messages:[{id:'m1',role:'unknown',text:payload,source:'event:1',completeness:'possibly_truncated'}],relations:[],criteria:[],artifacts:[]};
      return new Response(JSON.stringify(data));
    };
    await import('../../src/agent_hub/dashboard_static/app.mjs');
    await settle();
    assert.equal(requests[0],'strip');
    assert.equal(requests[1],'/dashboard/session');
    assert.ok(nodes.get('state-filter').children.some(x=>x.value === 'succeeded'));
    assert.ok(!nodes.get('state-filter').children.some(x=>x.value === 'completed'));
    assert.match(nodes.get('agent-count').textContent,/不可用/);
    nodes.get('refresh').listeners.click(); await settle();
    assert.equal(nodes.get('agent-count').textContent,'1 个已配置执行器');
    // Lists keep refreshing even before any task has been selected.
    taskItems = [{...task,id:'task-2',title:'new task'},task];
    await tick(5000);
    assert.equal(nodes.get('task-list').children.length,2);
    taskItems = [task]; await tick(5000);
    const taskButton = nodes.get('task-list').children[0];
    await taskButton.listeners.click();
    await settle();
    assert.equal(nodes.get('task-title').textContent,payload);
    const rendered = [];
    function walk(element) { rendered.push(element); for (const child of element.children) walk(child); }
    walk(nodes.get('content'));
    assert.ok(rendered.some(x=>x.textContent === payload));
    assert.ok(rendered.some(x=>x.textContent === '来源身份未记录'));
    assert.ok(rendered.some(x=>x.textContent === '可能已截断'));
    assert.ok(!rendered.some(x=>x.tagName === 'img'));
    assert.ok(timers.some(x=>x.delay === 2500));
    const messageNode = nodes.get('content').children[0];
    eventItems = []; taskState = 'running';
    await tick(2500);
    assert.equal(nodes.get('status-line').children[0].textContent,'执行 · 执行中');
    assert.equal(nodes.get('content').children[0],messageNode,'no-event state transition preserves message DOM');
    tabs[1].listeners.click();
    const rawEvent = nodes.get('content').children.find(x=>x.tagName === 'details');
    rawEvent.open = true; const summary = rawEvent.children[0]; summary.focus();
    await tick(2500);
    assert.equal(nodes.get('content').children.find(x=>x.tagName === 'details'),rawEvent);
    assert.equal(rawEvent.open,true); assert.equal(document.activeElement,summary);
    eventItems = [{seq:2,kind:'new',data:{text:'incremental'},timestamp:3}];
    await tick(2500);
    assert.equal(rawEvent.open,true); assert.equal(document.activeElement,summary);
    assert.equal(nodes.get('content').children.filter(x=>x.tagName === 'details').length,2);
    // Refreshing the selected task from Events must clear the temporary loading view.
    nodes.get('refresh').listeners.click(); await settle();
    const afterRefresh = [];
    function collect(element) { afterRefresh.push(element); for (const child of element.children) collect(child); }
    collect(nodes.get('content'));
    assert.ok(!afterRefresh.some(x=>x.textContent === '正在读取任务'));
    assert.ok(nodes.get('content').children.some(x=>x.tagName === 'details'));
    nodes.get('refresh').listeners.click(); await settle();
    const secondRefresh = [];
    function collectAgain(element) { secondRefresh.push(element); for (const child of element.children) collectAgain(child); }
    collectAgain(nodes.get('content'));
    assert.ok(!secondRefresh.some(x=>x.textContent === '正在读取任务'));
    const focusedTask = nodes.get('task-list').children[0]; focusedTask.focus();
    taskItems = [{...task,state:'running',title:'updated title'}]; await tick(5000);
    assert.equal(document.activeElement.dataset.taskId,'task-1');
    assert.notEqual(document.activeElement,focusedTask,'list redraw restores task focus');
  } finally {
    for (const [key,value] of Object.entries(original)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value; }
  }
});
