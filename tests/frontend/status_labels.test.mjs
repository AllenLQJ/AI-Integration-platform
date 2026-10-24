import test from 'node:test';
import assert from 'node:assert/strict';
import {
  isRemoteUncertain, stateLabel, verificationLabel, workStatusLabel,
} from '../../src/agent_hub/dashboard_static/status_labels.mjs';

test('interrupted labels branch on remote evidence vs local CLI', () => {
  assert.equal(
    stateLabel({state: 'interrupted', agent: 'codex'}),
    '本地执行已中断');
  assert.equal(
    stateLabel({state: 'interrupted', agent: 'claude', delivery: 'accepted'}),
    '本地执行已中断');
  assert.equal(
    stateLabel({
      state: 'interrupted', agent: 'grok', delivery: 'accepted', has_baseline: true,
    }),
    '连接中断／远端状态未知');
  assert.equal(
    stateLabel({
      state: 'interrupted', agent: 'muse', has_remote_session: true,
    }),
    '连接中断／远端状态未知');
  assert.equal(
    stateLabel({
      state: 'interrupted', agent: 'worker', cancel_scope: 'remote_session',
      delivery: 'accepted',
    }),
    '连接中断／远端状态未知');
  // Remote agent without delivery/session evidence must not claim remote uncertainty.
  assert.equal(
    stateLabel({state: 'interrupted', agent: 'grok'}),
    '本地执行已中断');
  assert.equal(
    stateLabel({state: 'delivery_unknown', agent: 'cursor_cli'}),
    '投递未确认');
  assert.equal(
    stateLabel({
      state: 'delivery_unknown', agent: 'grokbot', has_baseline: true,
    }),
    '投递未确认／远端状态未知');
  assert.equal(isRemoteUncertain({agent: 'codex', delivery: 'accepted'}), false);
  assert.equal(isRemoteUncertain({agent: 'grok', delivery: 'accepted'}), true);
  assert.equal(verificationLabel({verification_label: 'rework_required'}), '需要返工');
  assert.equal(verificationLabel({verification_state: 'passed'}), '验收通过');
});

test('work_status mirrors remote-uncertain interrupted copy', () => {
  assert.equal(
    workStatusLabel({
      work_status: 'interrupted', state: 'interrupted', agent: 'codex',
    }),
    '本地执行已中断');
  assert.equal(
    workStatusLabel({
      work_status: 'interrupted', state: 'interrupted', agent: 'grok', delivery: 'accepted',
    }),
    '连接中断／远端状态未知');
  assert.equal(
    workStatusLabel({
      work_status: 'delivery_unknown', state: 'delivery_unknown', agent: 'grokbot', has_baseline: true,
    }),
    '投递未确认／远端状态未知');
  assert.equal(workStatusLabel({work_status: 'awaiting_verification'}), '等待验收');
  assert.equal(workStatusLabel({work_status: 'completed'}), '已完成');
});

