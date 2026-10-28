import test from 'node:test';
import assert from 'node:assert/strict';
import {stateOutcome, tooltip} from '../../src/agent_hub/dashboard_static/viz.mjs';

class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.style = {}; this.hidden = false; this.textContent = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(key, value) { this.attributes[key] = value; }
}
globalThis.document = {createElement: tag => new Node(tag)};

test('execution states map to the four drawn outcomes', () => {
  const key = state => stateOutcome(state).key;
  assert.deepEqual(['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted', 'delivery_unknown', 'running', 'queued', 'waiting_for_user']
    .map(key), ['succeeded', 'failed', 'failed', 'other', 'other', 'other', 'active', 'active', 'active']);
});

test('the tooltip stays on screen: it flips above the pointer near the bottom and clamps at the right', () => {
  globalThis.innerWidth = 800; globalThis.innerHeight = 600;
  const box = new Node('div');
  const tip = tooltip(box), element = box.children[0];
  element.offsetHeight = 60;
  tip.show(['a', 'b'], {clientX: 150, clientY: 70});
  assert.equal(element.hidden, false);
  assert.deepEqual([element.style.left, element.style.top], ['162px', '82px']);
  // 560px down a 600px window: below would leave the screen, so it goes above (560 - 12 - 60).
  tip.show(['a'], {clientX: 150, clientY: 560});
  assert.equal(element.style.top, '488px');
  // Near the right edge it is pulled back so the tooltip still fits.
  tip.show(['a'], {clientX: 790, clientY: 70});
  assert.equal(element.style.left, '610px');
  // Keyboard focus has no pointer position: it anchors below the focused mark.
  tip.show(['a'], {target: {getBoundingClientRect: () => ({left: 30, bottom: 200})}});
  assert.deepEqual([element.style.left, element.style.top], ['30px', '206px']);
  tip.hide();
  assert.equal(element.hidden, true);
});

test('with a page body, every chart shares one tooltip under <body>, outside any clipping panel', () => {
  const body = new Node('body');
  globalThis.document = {createElement: tag => new Node(tag), body};
  try {
    const first = new Node('section'), second = new Node('section');
    const a = tooltip(first), b = tooltip(second);
    assert.equal(body.children.length, 1);
    assert.deepEqual([first.children.length, second.children.length], [0, 0]);
    a.show(['a'], {clientX: 10, clientY: 10});
    assert.equal(body.children[0].hidden, false);
    // Drawing another chart hides a tooltip whose marks are gone.
    tooltip(new Node('section'));
    assert.equal(body.children[0].hidden, true);
    b.show(['b'], {clientX: 10, clientY: 10});
    assert.equal(body.children[0].hidden, false);
  } finally {
    globalThis.document = {createElement: tag => new Node(tag)};
  }
});
