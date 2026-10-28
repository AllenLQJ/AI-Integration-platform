import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const css = readFileSync(new URL('../../src/agent_hub/dashboard_static/style.css', import.meta.url), 'utf8');

test('dashboard colours come from semantic tokens that dark mode redefines', () => {
  const rootEnd = css.indexOf('}');
  const root = css.slice(0, rootEnd);
  const darkStart = css.indexOf('@media (prefers-color-scheme: dark)');
  assert.ok(darkStart > rootEnd, 'dark palette follows :root');
  const darkEnd = css.indexOf('\n}', darkStart);
  const dark = css.slice(darkStart, darkEnd);
  // An explicit dark choice reuses the system-dark tokens verbatim, so the two cannot drift apart.
  const forcedStart = css.indexOf(':root[data-theme="dark"] {', darkEnd);
  assert.ok(forcedStart > darkEnd, 'a forced dark theme follows the system one');
  const forcedEnd = css.indexOf('\n}', forcedStart);
  const declarations = block => [...block.matchAll(/([\w-]+):([^;]+);/g)].map(m => m[1] + ':' + m[2].trim());
  assert.deepEqual(declarations(css.slice(forcedStart, forcedEnd)), declarations(dark.slice(dark.indexOf('{', dark.indexOf(':root')))));
  assert.match(dark, /:root:not\(\[data-theme="light"\]\)/, 'an explicit light choice overrides a dark system');
  const rules = css.slice(forcedEnd);
  const tokens = new Map([...root.matchAll(/(--[\w-]+):([^;]+);/g)].map(m => [m[1], m[2].trim()]));
  const redefined = new Set([...dark.matchAll(/(--[\w-]+):/g)].map(m => m[1]));
  // Every colour token has its own dark value (no automatic flip), and every token used is defined.
  const colours = [...tokens].filter(([, value]) => /^#[0-9a-fA-F]{3,8}$/.test(value)).map(([name]) => name);
  assert.ok(colours.length >= 20, 'the palette is defined as tokens');
  for (const name of colours) assert.ok(redefined.has(name), `${name} has no dark value`);
  for (const name of redefined) assert.ok(tokens.has(name), `${name} is only defined for dark mode`);
  for (const [, name] of rules.matchAll(/var\((--[\w-]+)[,)]/g)) assert.ok(tokens.has(name), `${name} is undefined`);
  // Rules never hard-code a colour: shadows, glows and tints all go through tokens.
  assert.deepEqual([...rules.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map(m => m[0]), []);
  // Named colours as values (not `white-space`) and rgb() literals are ruled out too.
  assert.doesNotMatch(rules, /[:,(]\s*(?:white|black)\s*[;,)]|rgba?\(/);
});

test('motion is off when the viewer asks for reduced motion', () => {
  const block = css.slice(css.indexOf('@media(prefers-reduced-motion:reduce)'));
  assert.match(block, /animation:none!important/);
  assert.match(block, /transition:none!important/);
  assert.match(block, /view-transition-new\(root\)/);
});
