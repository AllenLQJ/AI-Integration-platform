import test from 'node:test';
import assert from 'node:assert/strict';
import {countUp, easeOut, prefersReducedMotion, stagger, tween, viewTransition} from '../../src/agent_hub/dashboard_static/motion.mjs';

const reduce = on => { globalThis.matchMedia = query => ({matches: on && query.includes('reduce')}); };

test('easing lands exactly and tween clamps to its target', () => {
  assert.equal(easeOut(0), 0);
  assert.equal(easeOut(1), 1);
  assert.equal(easeOut(2), 1);
  assert.ok(easeOut(.5) > .5, 'ease-out runs ahead early');
  assert.equal(tween(0, 80, 0, 700), 0);
  assert.equal(tween(0, 80, 700, 700), 80);
  assert.equal(tween(0, 80, 9999, 700), 80);
  assert.equal(tween(0, 80, 10, 0), 80);
});

test('count-up keeps prefix and suffix, steps through frames and ends on the exact text', () => {
  reduce(false);
  const frames = [], el = {textContent: ''};
  let clock = 0;
  countUp(el, '65%', {duration: 100, raf: fn => frames.push(fn), now: () => clock});
  assert.equal(el.textContent, '0%');
  clock = 50; frames.shift()();
  assert.match(el.textContent, /^\d+%$/);
  assert.ok(Number(el.textContent.slice(0, -1)) > 0 && Number(el.textContent.slice(0, -1)) < 65);
  clock = 100; frames.shift()();
  assert.equal(el.textContent, '65%');
  assert.equal(frames.length, 0, 'no frame is scheduled after landing');
  // Decimals keep their precision.
  const decimal = {textContent: ''}, queue = [];
  let tick = 0;
  countUp(decimal, '4.6 次', {duration: 10, raf: fn => queue.push(fn), now: () => tick});
  assert.equal(decimal.textContent, '0.0 次');
  tick = 10; queue.shift()();
  assert.equal(decimal.textContent, '4.6 次');
});

test('count-up shows non-numbers and reduced-motion values immediately, and can be stopped', () => {
  reduce(false);
  const dash = {textContent: ''};
  countUp(dash, '—', {raf: () => assert.fail('no animation for text')});
  assert.equal(dash.textContent, '—');
  reduce(true);
  assert.equal(prefersReducedMotion(), true);
  const quiet = {textContent: ''};
  countUp(quiet, '12', {raf: () => assert.fail('no animation under reduced motion')});
  assert.equal(quiet.textContent, '12');
  reduce(false);
  const frames = [], el = {textContent: ''};
  const stop = countUp(el, '30', {duration: 100, raf: fn => frames.push(fn), now: () => 0});
  stop();
  assert.equal(el.textContent, '30');
  frames.shift()();
  assert.equal(el.textContent, '30', 'a stopped count-up does not overwrite the value');
});

test('stagger numbers elements for their entrance delay', () => {
  const make = () => ({style: {props: {}, setProperty(key, value) { this.props[key] = value; }}, classList: {set: new Set(), add(c) { this.set.add(c); }}});
  const nodes = [make(), make(), make()];
  stagger(nodes, 2);
  assert.deepEqual(nodes.map(n => n.style.props['--i']), ['2', '3', '4']);
  assert.ok(nodes.every(n => n.classList.set.has('enter')));
});

test('view transitions wrap the update where supported, and fall back to a plain update', () => {
  reduce(false);
  const calls = [];
  viewTransition(() => calls.push('update'), {startViewTransition: fn => { calls.push('transition'); fn(); return {}; }});
  assert.deepEqual(calls, ['transition', 'update']);
  calls.length = 0;
  viewTransition(() => calls.push('update'), {});
  assert.deepEqual(calls, ['update']);
  reduce(true);
  calls.length = 0;
  viewTransition(() => calls.push('update'), {startViewTransition: () => assert.fail('no transition under reduced motion')});
  assert.deepEqual(calls, ['update']);
  reduce(false);
});
