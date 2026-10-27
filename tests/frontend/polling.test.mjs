import test from 'node:test';
import assert from 'node:assert/strict';

// Minimal DOM surface; this file runs in its own process so the app module starts fresh.
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.style = {}; this.parentElement = null; this.scrollLeft = 0; this.scrollTop = 0; this.children = []; this.listeners = {}; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.hidden = false; this.dataset = {}; this.classes = new Set(); this.classList = {add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),contains:x=>this.classes.has(x),toggle:(x,force)=>{ const next = force ?? !this.classes.has(x); next ? this.classes.add(x) : this.classes.delete(x); return next; }}; }
  get firstElementChild() { return this.children[0] || null; }
  append(...items) { for (const item of items) { item.remove(); item.parentElement = this; this.children.push(item); } }
  replaceChildren(...items) { for (const item of this.children) item.parentElement = null; this.children = []; this.append(...items); }
  remove() { if (this.parentElement) { const parent = this.parentElement; parent.children = parent.children.filter(x=>x!==this); this.parentElement = null; } }
  insertBefore(item, before) { item.remove(); item.parentElement = this; const index = this.children.indexOf(before); this.children.splice(index < 0 ? this.children.length : index, 0, item); }
  scrollIntoView() {}
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() { document.activeElement = this; }
}
const flush = () => new Promise(resolve=>setImmediate(resolve));

test('settled chains back off, hidden tabs stop polling and visible tabs resume at once', async () => {
  const nodes = new Map(), timers = [], requests = [], documentListeners = {};
  let activeCount = 0, verification = 'passed', taskState = 'succeeded';
  const summary = () => ({id:'task-1',root_task_id:'task-1',latest_task_id:'task-1',title:'settled work',agent:'echo',agents:['echo'],
    task_count:1,active_count:activeCount,state:taskState,verification_state:verification,workspace_label:'w',created:1,updated:2,mode:'read_only'});
  const pending = delay => timers.filter(item=>!item.cancelled && !item.used && (delay == null || item.delay === delay));
  async function settle() { for (let i = 0; i < 8; i++) await flush(); }
  async function run(delay) {
    const due = pending(delay);
    assert.ok(due.length, `timer ${delay} scheduled`);
    for (const item of due) { item.used = true; await item.fn(); }
    await settle();
  }
  const original = Object.fromEntries(['document','window','history','location','fetch','Option','addEventListener','innerWidth','setTimeout','clearTimeout','matchMedia'].map(key=>[key,globalThis[key]]));
  try {
    globalThis.document = {visibilityState:'visible',getElementById:id=>{ if (!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id); },
      createElement:tag=>new Element(tag),createElementNS:(_ns,tag)=>new Element(tag),querySelectorAll:()=>[],
      addEventListener:(key,fn)=>{ documentListeners[key] = fn; }};
    globalThis.window = {location:{hash:'',pathname:'/dashboard/',search:''},history:{replaceState:()=>{}}};
    globalThis.Option = class extends Element { constructor(title,value) { super('option'); this.textContent = title; this.value = value; } };
    globalThis.matchMedia = ()=>({matches:true}); globalThis.addEventListener = ()=>{}; globalThis.innerWidth = 1200;
    globalThis.setTimeout = (fn,delay)=>{ const item = {fn,delay}; timers.push(item); return item; };
    globalThis.clearTimeout = timer=>{ if (timer) timer.cancelled = true; };
    globalThis.fetch = async url=>{
      requests.push(url);
      const task = summary();
      const data = url.endsWith('/agents') ? {items:[{id:'echo'}]}
        : url.includes('/events?') ? {events:[{seq:1,kind:'progress',data:{message:'done'},timestamp:2}],next_seq:1,has_more:false}
        : url.includes('/chains?') ? {items:[task],next_offset:null}
        : url.includes('/chains/') ? {chain:task,nodes:[task],edges:[],messages:[],warnings:[],truncated:false}
        : {task,messages:[],relations:[],criteria:[],artifacts:[],verification_checks:[]};
      return new Response(JSON.stringify(data));
    };
    await import('../../src/agent_hub/dashboard_static/app.mjs');
    await settle();
    await nodes.get('task-list').children[0].listeners.click();
    await settle();
    // A finished, verified chain polls its graph and execution every 15s instead of 2.5s.
    assert.equal(pending(2500).length, 0);
    assert.equal(pending(15000).length, 2);
    assert.equal(pending(5000).length, 1, 'list keeps its cadence so new chains appear');

    // New work seen by the chain poll restores the fast cadence and pulls the
    // sleeping execution poll forward instead of leaving it on the 15s timer.
    activeCount = 1; taskState = 'running';
    const chainTimer = pending(15000).find(item=>String(item.fn).includes('pollChain'));
    const detailTimer = pending(15000).find(item=>item !== chainTimer);
    chainTimer.used = true; await chainTimer.fn(); await settle();
    assert.ok(detailTimer.cancelled, 'idle execution poll is pulled forward');
    assert.equal(pending(0).length, 1);
    await run(0);
    assert.equal(pending(2500).length, 2);
    assert.equal(pending(15000).length, 0);

    // Verification that is still pending also keeps the fast cadence.
    activeCount = 0; taskState = 'succeeded'; verification = 'verifying';
    await run(2500);
    assert.equal(pending(15000).length, 0);

    // Hidden tabs finish in-flight polls but schedule nothing new.
    document.visibilityState = 'hidden';
    for (const delay of [2500, 5000, 15000]) if (pending(delay).length) await run(delay);
    assert.equal(pending().length, 0);
    const before = requests.length;

    // Becoming visible resumes list, chain and execution polling immediately.
    document.visibilityState = 'visible';
    documentListeners.visibilitychange();
    await settle();
    const resumed = requests.slice(before);
    assert.ok(resumed.some(url=>url.includes('/chains?')), resumed.join(' '));
    assert.ok(resumed.some(url=>url.includes('/chains/task-1')), resumed.join(' '));
    assert.ok(resumed.some(url=>url.endsWith('/tasks/task-1')), resumed.join(' '));
    assert.ok(pending().length >= 3);

    // A second visibility event does not start duplicate loops.
    const scheduled = pending().length;
    documentListeners.visibilitychange();
    await settle();
    assert.equal(pending().length, scheduled);
  } finally {
    for (const [key,value] of Object.entries(original)) globalThis[key] = value;
  }
});
