import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// The workbench script lives in daemon.py as a plain (escape-free) Python string.
const daemon = readFileSync(new URL('../../src/agent_hub/daemon.py', import.meta.url), 'utf8');
const SCRIPT = daemon.match(/WORKBENCH_SCRIPT = '''([\s\S]*?)'''/)[1];

class Element {
  constructor(tag, {className = '', id = '', text = ''} = {}) {
    this.tagName = tag.toUpperCase(); this.className = className; this.id = id; this.textContent = text;
    this.children = []; this.parentElement = null; this.dataset = {}; this.listeners = {}; this.hidden = false; this.disabled = false;
  }
  get firstElementChild() { return this.children[0] || null; }
  append(...items) { for (const item of items) { item.parentElement = this; this.children.push(item); } }
  insertBefore(item, before) { item.parentElement = this; const index = this.children.indexOf(before); this.children.splice(index < 0 ? this.children.length : index, 0, item); }
  addEventListener(kind, fn) { this.listeners[kind] = fn; }
  matches(selector) { return selector.startsWith('#') ? this.id === selector.slice(1) : this.className.split(' ').includes(selector.slice(1)); }
  querySelector(selector) {
    for (const child of this.children) { if (child.matches(selector)) return child; const found = child.querySelector(selector); if (found) return found; }
    return null;
  }
}
const flush = () => new Promise(resolve => setImmediate(resolve));

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
  const original = Object.fromEntries(['document', 'fetch', 'setTimeout', 'location'].map(key => [key, globalThis[key]]));
  try {
    globalThis.document = {
      body: page, createElement: tag => new Element(tag),
      querySelector: selector => page.querySelector(selector),
      querySelectorAll: selector => selector === '[data-task]' ? [section] : selector === '[data-verification]' ? [panel] : [],
    };
    globalThis.location = {reload() { throw new Error('unexpected reload'); }};
    globalThis.setTimeout = () => 0;  // Live polling is not under test here.
    globalThis.fetch = async url => { requests.push(url); return new Response(JSON.stringify(pages.shift())); };
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
