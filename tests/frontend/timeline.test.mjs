import test from 'node:test';
import assert from 'node:assert/strict';
import {
  TimelineExpansion, timelineRecords, coverageNotes, SHORT_MESSAGE_CHARS,
} from '../../src/agent_hub/dashboard_static/timeline.mjs';

test('individual and bulk toggles with future arrivals and chain isolation', () => {
  const expansion = new TimelineExpansion();
  const short = {id: 'm1', kind: 'message', text: 'hi'};
  const long = {id: 'm2', kind: 'message', text: 'x'.repeat(SHORT_MESSAGE_CHARS + 1)};
  const op = {id: 'op1', kind: 'command', text: '', operation: {command_display: 'ls'}};
  assert.equal(expansion.isOpen('c1', short), true);
  assert.equal(expansion.isOpen('c1', long), false);
  assert.equal(expansion.isOpen('c1', op), false);
  expansion.setOpen('c1', 'm2', true);
  assert.equal(expansion.isOpen('c1', long), true);
  expansion.setAll('c1', false);
  assert.equal(expansion.isOpen('c1', short), false);
  assert.equal(expansion.isOpen('c1', long), false);
  const arrival = {id: 'm3', kind: 'message', text: 'new short'};
  assert.equal(expansion.isOpen('c1', arrival), false, 'bulk collapse applies to future arrivals');
  expansion.setOpen('c1', 'm3', true);
  assert.equal(expansion.isOpen('c1', arrival), true, 'later individual override survives');
  expansion.setAll('c1', true);
  assert.equal(expansion.isOpen('c1', op), true);
  const other = {id: 'm1', kind: 'message', text: 'hi'};
  assert.equal(expansion.isOpen('c2', other), true, 'default on other chain');
  expansion.setAll('c2', false);
  assert.equal(expansion.isOpen('c1', short), true, 'cross-chain isolation');
  assert.equal(expansion.isOpen('c2', other), false);
  expansion.openTarget('c1', 'only-target');
  assert.equal(expansion.isOpen('c1', {id: 'only-target', kind: 'command'}), true);
  assert.equal(expansion.isOpen('c1', op), true, 'openTarget does not clear bulk/other items');
});

test('legacy messages fallback and coverage notes', () => {
  const legacy = timelineRecords({messages: [{id: 'a', role: 'user', text: 'hello', task_id: 't1', completeness: 'complete'}]});
  assert.equal(legacy.length, 1);
  assert.equal(legacy[0].kind, 'message');
  assert.equal(legacy[0].provenance, 'legacy_summary');
  const modern = timelineRecords({timeline: [{id: 'x', kind: 'command'}], messages: [{id: 'a'}]});
  assert.equal(modern.length, 1);
  assert.equal(modern[0].kind, 'command');
  assert.deepEqual(timelineRecords({timeline: [], messages: [{id: 'a', role: 'user', text: 'hi'}]}), []);
  const unknown = timelineRecords({messages: [{id: 'u', role: 'unknown', text: 'x', task_id: 't', agent: 'cursor_cli'}]});
  assert.equal(unknown[0].title, 'Cursor · 任务消息');
  assert.equal(unknown[0].role_missing, true);
  const withActor = timelineRecords({messages: [{id: 'b', role: 'assistant', text: 'y', actor: 'ops', recipient: 'cursor'}]});
  assert.equal(withActor[0].actor, 'ops');
  assert.equal(withActor[0].recipient, 'cursor');
  assert.deepEqual(coverageNotes({capture_coverage: {note: '仅有消息'}}), ['仅有消息']);
  assert.deepEqual(coverageNotes({capture_coverage: [{note: 'a'}, {note: 'a'}, {note: 'b'}]}), ['a', 'b']);
});
