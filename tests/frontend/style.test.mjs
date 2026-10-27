import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const css = readFileSync(new URL('../../src/agent_hub/dashboard_static/style.css', import.meta.url), 'utf8');

test('dashboard colours come from tokens that dark mode redefines', () => {
  const rootEnd = css.indexOf('}');
  const root = css.slice(0, rootEnd);
  const darkStart = css.indexOf('@media (prefers-color-scheme: dark)');
  assert.ok(darkStart > rootEnd, 'dark palette follows :root');
  const darkEnd = css.indexOf('\n}', darkStart);
  const dark = css.slice(darkStart, darkEnd);
  const rules = css.slice(darkEnd);
  const defined = new Set([...root.matchAll(/(--[\w-]+):/g)].map(m => m[1]));
  const redefined = new Set([...dark.matchAll(/(--[\w-]+):/g)].map(m => m[1]));
  // Every colour token has a dark value, and every token used is defined.
  for (const name of defined) if (name.startsWith('--p-') || ['--ink', '--muted', '--blue', '--line', '--soft', '--green'].includes(name))
    assert.ok(redefined.has(name), `${name} has no dark value`);
  for (const [, name] of rules.matchAll(/var\((--[\w-]+)\)/g)) assert.ok(defined.has(name), `${name} is undefined`);
  // Literal colours outside the palette are limited to alpha shadows and dark chips under white text.
  const literals = [...rules.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map(m => m[0].toLowerCase());
  for (const value of literals) assert.ok(value.length === 9 || ['#1d1d1f', '#25272a'].includes(value), value);
  assert.doesNotMatch(rules, /background(?:-color)?:\s*white\b/);
});
