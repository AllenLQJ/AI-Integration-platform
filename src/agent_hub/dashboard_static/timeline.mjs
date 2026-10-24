/** Pure timeline expansion state and card helpers for the collaboration view. */

export const SHORT_MESSAGE_CHARS = 420;

export class TimelineExpansion {
  constructor() {
    this.chains = new Map();
  }

  _bucket(chainId) {
    if (!this.chains.has(chainId)) {
      this.chains.set(chainId, {bulk: null, items: new Map()});
    }
    return this.chains.get(chainId);
  }

  defaultOpen(entry) {
    if (!entry) return false;
    if (entry.kind === 'message') {
      const text = typeof entry.text === 'string' ? entry.text : '';
      return text.length <= SHORT_MESSAGE_CHARS;
    }
    return false;
  }

  isOpen(chainId, entry) {
    const bucket = this._bucket(chainId);
    const id = entry?.id;
    if (id && bucket.items.has(id)) return bucket.items.get(id);
    if (bucket.bulk !== null) return bucket.bulk;
    return this.defaultOpen(entry);
  }

  setOpen(chainId, id, open) {
    if (!chainId || !id) return;
    this._bucket(chainId).items.set(id, Boolean(open));
  }

  setAll(chainId, open) {
    if (!chainId) return;
    const bucket = this._bucket(chainId);
    bucket.bulk = Boolean(open);
    // Future arrivals follow bulk; clear per-item so bulk applies uniformly to loaded set.
    bucket.items.clear();
  }

  openTarget(chainId, id) {
    if (!chainId || !id) return;
    this._bucket(chainId).items.set(id, true);
  }
}

export function timelineRecords(chain) {
  if (!chain) return [];
  // Empty timeline is authoritative: do not fall back to legacy messages.
  if (Array.isArray(chain.timeline)) return chain.timeline;
  if (Array.isArray(chain.messages)) {
    const agentLabels = {cursor_cli:'Cursor',cursor:'Cursor',codex:'Codex',claude:'Claude',muse:'Muse',grokbot:'Grok',grok:'Grok'};
    return chain.messages.map(message => {
      const knownRole = ['user', 'assistant', 'system', 'tool'].includes(message.role);
      const roleMissing = !knownRole;
      const agentTitle = agentLabels[message.agent]
        ? `${agentLabels[message.agent]} · 任务消息`
        : (message.agent ? `${message.agent} · 任务消息` : '任务消息');
      const title = message.role === 'user' ? '任务输入'
        : message.role === 'assistant' ? '模型回复'
        : message.role === 'system' ? '系统记录'
        : message.role === 'tool' ? '工具记录'
        : agentTitle;
      const actor = message.actor || (message.role === 'assistant' ? message.agent
        : (knownRole ? message.role : null));
      return {
        id: message.id,
        task_id: message.task_id,
        kind: 'message',
        timestamp: message.timestamp,
        source: message.source,
        completeness: message.completeness,
        provenance: 'legacy_summary',
        actor,
        recipient: message.recipient || null,
        title,
        preview: (message.text || '').slice(0, 180),
        text: message.text || '',
        role: message.role,
        role_missing: roleMissing,
        agent: message.agent,
        status: null,
        operation: null,
      };
    });
  }
  return [];
}

export function coverageNotes(chain) {
  const items = [];
  const coverage = chain?.capture_coverage;
  const list = Array.isArray(coverage) ? coverage : coverage ? [coverage] : [];
  for (const item of list) {
    if (item?.note) items.push(item.note);
  }
  return [...new Set(items)];
}

export function kindLabel(kind) {
  return ({
    message: '消息',
    file_read: '读文件',
    command: '命令',
    test: '测试',
    file_change: '文件变更',
    system: '系统',
  })[kind] || '记录';
}

export function provenanceLabel(value) {
  return ({
    executor_observed: '执行器观测',
    controller_reported: '总控上报',
    legacy_summary: '兼容摘要',
  })[value] || '来源未标注';
}

export function statusLabel(value) {
  return ({started: '进行中', completed: '已完成', failed: '失败', unknown: '状态未知'})[value] || '';
}
