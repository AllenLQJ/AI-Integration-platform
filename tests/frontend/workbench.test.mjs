import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// The daemon inlines this file into the page, so it has to stay free of backslashes and "</".
const SCRIPT = readFileSync(new URL('../../src/agent_hub/dashboard_static/workbench.mjs', import.meta.url), 'utf8');

class Element {
  constructor(tag, {className = '', id = '', text = ''} = {}) {
    this.tagName = tag.toUpperCase(); this.className = className; this.id = id; this.textContent = text;
    this.children = []; this.parentElement = null; this.dataset = {}; this.listeners = {}; this.hidden = false; this.disabled = false;
  }
  get firstElementChild() { return this.children[0] || null; }
  append(...items) { for (const item of items) { item.parentElement = this; this.children.push(item); } }
  insertBefore(item, before) { item.parentElement = this; const index = this.children.indexOf(before); this.children.splice(index < 0 ? this.children.length : index, 0, item); }
  addEventListener(kind, fn) { this.listeners[kind] = fn; }
  remove() { if (this.parentElement) { const parent = this.parentElement; parent.children = parent.children.filter(x => x !== this); this.parentElement = null; } }
  matches(selector) { return selector.startsWith('#') ? this.id === selector.slice(1) : this.className.split(' ').includes(selector.slice(1)); }
  querySelector(selector) {
    for (const child of this.children) { if (child.matches(selector)) return child; const found = child.querySelector(selector); if (found) return found; }
    return null;
  }
}
const flush = () => new Promise(resolve => setImmediate(resolve));

// One attempt with its timeline section and verification panel, as render_workbench emits them.
function workbench(seqs) {
  const pre = (seq, kind, data) => {
    const node = new Element('pre'); node.dataset.seq = String(seq);
    node.textContent = JSON.stringify({seq, kind, data, timestamp: seq}, null, 2); return node;
  };
  const section = new Element('section'); section.dataset.task = 'task-1';
  const note = new Element('p', {className: 'note'}), button = new Element('button', {className: 'older', text: '加载更早事件'});
  button.hidden = true; note.append(button);
  const events = new Element('div', {className: 'events'}); events.dataset.more = 'false';
  events.append(...seqs.map(seq => pre(seq, 'progress', {message: 'step ' + seq})));
  section.append(new Element('h3'), new Element('p'), note, events);
  section.children[1].append(new Element('span', {className: 'exec-state', text: 'running'}));
  const panel = new Element('section'); panel.dataset.verification = 'task-1';
  panel.append(new Element('span', {className: 'verification-state', text: 'not_requested'}),
               new Element('pre', {className: 'verdict', text: '{}'}), new Element('pre', {className: 'artifacts', text: '[]'}));
  const page = new Element('body'); page.dataset.work = 'task-1'; page.dataset.current = 'task-1';
  page.append(new Element('p', {id: 'connection'}), new Element('dd', {id: 'current-state'}), section, panel);
  return {page, section, panel, events, button};
}
function install(page, section, panel, overrides) {
  const listeners = {};
  globalThis.document = {
    visibilityState: 'visible', body: page, createElement: tag => new Element(tag),
    querySelector: selector => page.querySelector(selector),
    querySelectorAll: selector => selector === '[data-task]' ? [section] : selector === '[data-verification]' ? [panel] : [],
    addEventListener: (kind, fn) => { listeners[kind] = fn; },
  };
  globalThis.location = {reload() { throw new Error('unexpected reload'); }};
  Object.assign(globalThis, overrides);
  return listeners;
}
const GLOBALS = ['document', 'fetch', 'setTimeout', 'clearTimeout', 'location'];

test('workbench prepends older pages in order, labels events and hides the button at the start', async () => {
  const pre = (seq, kind, data) => {
    const node = new Element('pre'); node.dataset.seq = String(seq);
    node.textContent = JSON.stringify({seq, kind, data, timestamp: seq}, null, 2); return node;
  };
  const section = new Element('section'); section.dataset.task = 'task-1';
  const note = new Element('p', {className: 'note'}), button = new Element('button', {className: 'older', text: '加载更早事件'});
  note.append(button);
  const events = new Element('div', {className: 'events'}); events.dataset.more = 'true';
  events.append(pre(301, 'progress', {message: 'latest'}), pre(302, 'state', {state: 'running'}));
  section.append(new Element('h3'), new Element('p'), note, events);
  section.children[1].append(new Element('span', {className: 'exec-state', text: 'running'}));
  const panel = new Element('section'); panel.dataset.verification = 'task-1';
  panel.append(new Element('span', {className: 'verification-state', text: 'not_requested'}),
               new Element('pre', {className: 'verdict', text: '{}'}), new Element('pre', {className: 'artifacts', text: '[]'}));
  const page = new Element('body'); page.dataset.work = 'task-1'; page.dataset.current = 'task-1';
  page.append(new Element('p', {id: 'connection'}), new Element('dd', {id: 'current-state'}), section, panel);
  const requests = [];
  let pages = [
    {events: [{seq: 101, kind: 'message', data: {message: '第一行\n第二行'}, timestamp: 1},
              {seq: 102, kind: 'tool', data: {name: 'Read', status: 'success'}, timestamp: 2}], has_more: true},
    {events: [{seq: 7, kind: 'custom', data: {text: 'x'.repeat(200)}, timestamp: 0}], has_more: false},
  ];
  const original = Object.fromEntries(GLOBALS.map(key => [key, globalThis[key]]));
  try {
    install(page, section, panel, {
      setTimeout: () => 0,  // Live polling is not under test here.
      fetch: async url => { requests.push(url); return new Response(JSON.stringify(pages.shift())); },
    });
    new Function(SCRIPT)();

    await button.listeners.click(); await flush();
    assert.match(requests.at(-1), /\/work\/task-1\/events\?attempt_id=task-1&before_seq=301$/);
    assert.deepEqual(events.children.map(node => Number(node.dataset.seq)), [101, 102, 301, 302]);
    const [message, tool] = events.children;
    assert.equal(message.dataset.summary, '#101 消息：第一行 第二行');
    assert.equal(tool.dataset.summary, '#102 工具：Read · success');
    assert.equal(message.tabIndex, 0);
    // The element text stays the exact event JSON for raw inspection.
    assert.equal(JSON.parse(message.textContent).data.message, '第一行\n第二行');
    assert.equal(button.hidden, false);

    await button.listeners.click(); await flush();
    assert.match(requests.at(-1), /before_seq=101$/);
    assert.deepEqual(events.children.map(node => Number(node.dataset.seq)), [7, 101, 102, 301, 302]);
    assert.equal(Array.from(events.children[0].dataset.summary.split('：')[1]).length, 118);
    assert.ok(events.children[0].dataset.summary.endsWith('…'));
    assert.equal(button.hidden, true);
    assert.equal(events.dataset.more, 'false');
  } finally {
    for (const [key, value] of Object.entries(original)) globalThis[key] = value;
  }
});

test('workbench pauses while hidden, resumes on return and caps live event nodes', async () => {
  const {page, section, panel, events, button} = workbench([1, 2]);
  const timers = [], requests = [];
  const due = () => timers.filter(item => !item.used && !item.cancelled);
  const live = Array.from({length: 1005}, (_, index) => ({seq: 3 + index, kind: 'progress', data: {message: 'live ' + index}, timestamp: 3 + index}));
  const snapshot = {current_task_id: 'task-1', work_status: 'running', pending_actions: [],
                    attempts: [{task_id: 'task-1', state: 'running', verification: {state: 'not_requested'}, artifacts: []}]};
  const original = Object.fromEntries(GLOBALS.map(key => [key, globalThis[key]]));
  try {
    const listeners = install(page, section, panel, {
      setTimeout: (fn, delay) => { const item = {fn, delay}; timers.push(item); return item; },
      clearTimeout: item => { if (item && typeof item === 'object') item.cancelled = true; },
      fetch: async url => {
        requests.push(url);
        const body = url.includes('/snapshot') ? snapshot : url.includes('after_seq=2') ? {events: live, next_seq: 1007} : {events: [], next_seq: 1007};
        return new Response(JSON.stringify(body));
      },
    });
    new Function(SCRIPT)();
    assert.equal(due().length, 2, 'events and snapshot channels start');

    // While hidden, in-flight polls finish but nothing new is scheduled.
    document.visibilityState = 'hidden';
    for (const item of due()) { item.used = true; await item.fn(); }
    for (let i = 0; i < 8; i++) await flush();
    assert.equal(due().length, 0);
    assert.equal(requests.length, 2);

    // 1007 live nodes are capped at the newest 1000; "older" can reload the rest.
    assert.equal(events.children.length, 1000);
    assert.equal(Number(events.children[0].dataset.seq), 8);
    assert.equal(Number(events.children.at(-1).dataset.seq), 1007);
    assert.equal(events.dataset.more, 'true');
    assert.equal(button.hidden, false);

    // Becoming visible runs both skipped polls immediately, once.
    document.visibilityState = 'visible';
    listeners.visibilitychange(); listeners.visibilitychange();
    for (let i = 0; i < 8; i++) await flush();
    assert.equal(requests.length, 4, requests.join(' '));
    assert.ok(requests.slice(2).some(url => url.includes('after_seq=1007')));
    assert.ok(requests.slice(2).some(url => url.endsWith('/snapshot')));
  } finally {
    for (const [key, value] of Object.entries(original)) globalThis[key] = value;
  }
});
