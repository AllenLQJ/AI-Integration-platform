import test from 'node:test';
import assert from 'node:assert/strict';
import {createPalette, rank, score} from '../../src/agent_hub/dashboard_static/command.mjs';

class Node {
  constructor(tag, doc) {
    this.tagName = tag; this.doc = doc; this.children = []; this.attributes = {}; this.listeners = {};
    this.className = ''; this.textContent = ''; this.hidden = false; this.value = ''; this.id = '';
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(kind, fn) { this.listeners[kind] = fn; }
  focus() { this.doc.activeElement = this; }
  scrollIntoView() {}
  get text() { return this.textContent + this.children.map(child => child.text).join(''); }
}
function makeDoc() {
  const doc = {activeElement: null};
  doc.createElement = tag => new Node(tag, doc);
  doc.body = new Node('body', doc);
  return doc;
}
const key = (input, name) => {
  const event = {key: name, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }};
  input.listeners.keydown(event);
  return event;
};
const flush = (ms = 5) => new Promise(resolve => setTimeout(resolve, ms));

test('scores prefer substrings that start early, then tight in-order matches', () => {
  assert.equal(score('', 'anything'), 0);
  assert.ok(score('svc', 'Service.call') !== null, 'abbreviations match');
  assert.ok(score('serv', 'Service.call') > score('call', 'Service.call'), 'earlier substring wins');
  assert.ok(score('call', 'Service.call') > score('scl', 'Service.call'), 'substring beats subsequence');
  assert.ok(score('scl', 'Service.call') !== null, 'in-order characters match');
  assert.equal(score('lcs', 'Service.call'), null, 'out-of-order characters do not');
  assert.ok(score('重构', '重构 Service.call 分发逻辑') > 0);
});

test('rank filters, orders by score, keeps ties stable and caps the list', () => {
  const items = [{id: 'a1', title: '整理文档'}, {id: 'b2', title: '修复登录'}, {id: 'c3', title: '修复看板'}, {id: 'zz', title: '其他'}];
  assert.deepEqual(rank(items, '修复').map(i => i.id), ['b2', 'c3']);
  assert.deepEqual(rank(items, 'c3').map(i => i.id), ['c3'], 'ids match too');
  assert.deepEqual(rank(items, '').map(i => i.id), ['a1', 'b2', 'c3', 'zz']);
  assert.equal(rank(items, '', 2).length, 2);
});

test('the palette opens on the input, moves with arrows, runs on Enter and gives focus back', () => {
  const doc = makeDoc(), ran = [];
  const trigger = new Node('button', doc); trigger.focus();
  const items = [{id: 'x', title: '回到总览', run: () => ran.push('x')}, {id: 'y', title: '刷新数据', run: () => ran.push('y')}];
  const palette = createPalette({items: () => items, doc});
  const backdrop = doc.body.children[0], panel = backdrop.children[0], [input, list] = panel.children;
  assert.equal(backdrop.hidden, true);
  assert.deepEqual([panel.attributes.role, panel.attributes['aria-modal']], ['dialog', 'true']);
  palette.open();
  assert.equal(palette.isOpen(), true);
  assert.equal(doc.activeElement, input);
  assert.equal(list.children.length, 2);
  assert.equal(input.attributes['aria-activedescendant'], 'cmd-opt-0');
  key(input, 'ArrowDown');
  assert.deepEqual(list.children.map(o => o.attributes['aria-selected']), ['false', 'true']);
  key(input, 'ArrowDown');
  assert.equal(input.attributes['aria-activedescendant'], 'cmd-opt-0', 'wraps around');
  key(input, 'ArrowUp');
  assert.equal(key(input, 'Tab').prevented, true, 'focus stays inside');
  key(input, 'Enter');
  assert.deepEqual(ran, ['y']);
  assert.equal(palette.isOpen(), false);
  assert.equal(doc.activeElement, trigger);
});

test('typing filters, Escape closes without bubbling, and an empty result says so', () => {
  const doc = makeDoc();
  const palette = createPalette({items: () => [{id: 'a', title: '修复登录', run() {}}], doc});
  const [input, list] = doc.body.children[0].children[0].children;
  palette.open();
  input.value = '不存在的东西'; input.listeners.input();
  assert.equal(list.children.length, 1);
  assert.match(list.children[0].text, /没有匹配/);
  assert.equal(input.attributes['aria-activedescendant'], undefined);
  const escape = key(input, 'Escape');
  assert.equal(escape.stopped, true, 'the page does not also close its drawers');
  assert.equal(palette.isOpen(), false);
});

test('server results are merged once, and a stale search cannot overwrite a newer one', async () => {
  const doc = makeDoc(), calls = [];
  const remote = {'登录': [{id: 'r1', title: '登录页改版', run() {}}], '看板': [{id: 'r2', title: '看板重构', run() {}}]};
  const palette = createPalette({items: () => [{id: 'l1', title: '修复登录', run() {}}], doc, debounce: 1,
    search: async query => { calls.push(query); await flush(query === '登录' ? 20 : 1); return remote[query] || []; }});
  const [input, list] = doc.body.children[0].children[0].children;
  palette.open();
  input.value = '登录'; input.listeners.input();
  await flush(5);
  input.value = '看板'; input.listeners.input();
  await flush(40);
  assert.deepEqual(calls, ['登录', '看板']);
  assert.deepEqual(list.children.map(o => o.children[1].textContent), ['看板重构'], 'the slow, older search was dropped');
  input.value = '登录'; input.listeners.input();
  await flush(40);
  // The server's match starts at the first character, so it outranks the local one.
  assert.deepEqual(list.children.map(o => o.children[1].textContent), ['登录页改版', '修复登录']);
  palette.close();
});
