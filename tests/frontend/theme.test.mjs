import test from 'node:test';
import assert from 'node:assert/strict';
import {THEMES, applyTheme, nextTheme, readTheme, saveTheme} from '../../src/agent_hub/dashboard_static/theme.mjs';

const memory = () => {
  const data = new Map();
  return {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)), removeItem: key => data.delete(key), data};
};
const broken = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); }};

test('the toggle cycles system → light → dark → system', () => {
  assert.deepEqual(THEMES, ['system', 'light', 'dark']);
  assert.equal(nextTheme('system'), 'light');
  assert.equal(nextTheme('light'), 'dark');
  assert.equal(nextTheme('dark'), 'system');
  assert.equal(nextTheme('bogus'), 'light', 'an unknown value counts as system');
});

test('the choice is remembered, system clears it, and bad or blocked storage falls back to system', () => {
  const storage = memory();
  assert.equal(readTheme(storage), 'system');
  saveTheme('dark', storage);
  assert.equal(readTheme(storage), 'dark');
  saveTheme('system', storage);
  assert.equal(storage.data.size, 0, 'following the system stores nothing');
  storage.setItem('agent-hub:theme', 'purple');
  assert.equal(readTheme(storage), 'system');
  assert.equal(readTheme(broken), 'system');
  assert.doesNotThrow(() => saveTheme('dark', broken));
  assert.equal(readTheme(undefined), 'system');
});

test('applying a theme sets or clears the root override', () => {
  const attributes = new Map();
  const root = {setAttribute: (k, v) => attributes.set(k, v), removeAttribute: k => attributes.delete(k)};
  applyTheme('dark', root);
  assert.equal(attributes.get('data-theme'), 'dark');
  applyTheme('light', root);
  assert.equal(attributes.get('data-theme'), 'light');
  applyTheme('system', root);
  assert.equal(attributes.has('data-theme'), false);
  assert.doesNotThrow(() => applyTheme('dark', undefined));
});
