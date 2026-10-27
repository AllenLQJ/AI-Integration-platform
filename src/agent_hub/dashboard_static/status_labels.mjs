/** Shared execution/verification labels for list, detail, and tests. */

export const names = {
  queued: '排队中', running: '执行中', waiting_for_user: '等待用户', succeeded: '执行成功',
  completed: '已完成', failed: '失败', timed_out: '已超时', interrupted: '本地执行已中断',
  delivery_unknown: '投递未确认', cancelled: '已取消', pending: '待验收',
  awaiting_verification: '等待验收', verifying: '验收中', needs_review: '待人工审核', rework_required: '需要返工',
  passed: '验收通过', rejected: '验收未通过', not_requested: '未设置额外验收',
  completed_no_extra: '已完成／无需额外验收', not_configured: '未配置验收', unknown: '状态未知',
};

export const label = value => names[value] || value || '未记录';
export const verificationLabel = task => label(task?.verification_label || task?.verification_state);

/** Remote uncertain only when agent/scope is remote AND delivery/session evidence exists. */
export function isRemoteUncertain(task) {
  if (!task) return false;
  const agent = task.agent || '';
  const remoteAgent = agent === 'grok' || agent === 'grokbot' || agent === 'muse';
  const remoteScope = task.cancel_scope === 'remote_session' || task.cancel_scope === 'remote_hub_task';
  const evidence = task.delivery === 'accepted' || task.has_remote_session || task.has_baseline;
  return (remoteAgent || remoteScope) && evidence;
}

export function stateLabel(task) {
  if (!task) return '未记录';
  if (task.state === 'interrupted') {
    return isRemoteUncertain(task) ? '连接中断／远端状态未知' : '本地执行已中断';
  }
  if (task.state === 'delivery_unknown') {
    return isRemoteUncertain(task) ? '投递未确认／远端状态未知' : '投递未确认';
  }
  return label(task.state);
}

/** Comprehensive status: mirror remote-uncertain branching when work_status is an execution state. */
export function workStatusLabel(task) {
  if (!task) return '未记录';
  const status = task.work_status;
  if (status === 'interrupted' || status === 'delivery_unknown') {
    return stateLabel({ ...task, state: status });
  }
  return label(status);
}

