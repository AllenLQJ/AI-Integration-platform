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

test('the tooltip stays inside its box: it flips above the pointer near the bottom and clamps at the right', () => {
  const box = new Node('div');
  box.getBoundingClientRect = () => ({left: 100, top: 50, width: 400, height: 120});
  const tip = tooltip(box), element = box.children[0];
  element.offsetHeight = 60;
  tip.show(['a', 'b'], {clientX: 150, clientY: 70});
  assert.equal(element.hidden, false);
  assert.deepEqual([element.style.left, element.style.top], ['62px', '32px']);
  // 100px down a 120px box: below would overflow, so it goes above (100 - 12 - 60).
  tip.show(['a'], {clientX: 150, clientY: 150});
  assert.equal(element.style.top, '28px');
  // Near the right edge it is pulled back so 180px of tooltip still fits.
  tip.show(['a'], {clientX: 490, clientY: 70});
  assert.equal(element.style.left, '220px');
  tip.hide();
  assert.equal(element.hidden, true);
});
