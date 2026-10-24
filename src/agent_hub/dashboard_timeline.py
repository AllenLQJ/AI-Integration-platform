"""Bounded chronological timeline projection over messages and observed activities."""
from __future__ import annotations

import json

from .activity import relative_path_display
from .dashboard import _text, _safe


MAX_TIMELINE = 500
MAX_TEXT = 1000000
SHORT_MESSAGE_CHARS = 420


def _entry_size(entry):
    return len(json.dumps({
        'title': entry.get('title', ''),
        'preview': entry.get('preview', ''),
        'text': entry.get('text', ''),
        'operation': entry.get('operation') or {},
    }, ensure_ascii=False))


def _preview(text, limit=180):
    text = text.strip().replace('\n', ' ')
    return text if len(text) <= limit else text[: limit - 1] + '…'


_AGENT_LABELS = {
    'cursor_cli': 'Cursor', 'cursor': 'Cursor', 'codex': 'Codex', 'claude': 'Claude',
    'muse': 'Muse', 'grokbot': 'Grok', 'grok': 'Grok',
}


def _message_title(role, agent):
    if role in {'user', 'assistant', 'system', 'tool'}:
        return {'user': '任务输入', 'assistant': '模型回复', 'system': '系统记录', 'tool': '工具记录'}[role]
    label = _AGENT_LABELS.get(agent) or (agent if isinstance(agent, str) and agent else None)
    if label:
        return f'{label} · 任务消息'
    return '任务消息'


def _message_entry(message, task_id, agent=''):
    text = message.get('text') or ''
    role = message.get('role')
    actor = message.get('actor')
    if not isinstance(actor, str) or not actor.strip():
        actor = agent if role == 'assistant' else (role if role in {'user', 'system', 'tool'} else None)
    return dict(
        id=message['id'],
        task_id=task_id,
        kind='message',
        timestamp=message.get('timestamp'),
        source=message.get('source', 'unknown'),
        completeness=message.get('completeness', 'recorded'),
        provenance='legacy_summary' if str(message.get('source', '')).startswith('task.') else 'executor_observed',
        actor=actor,
        recipient=message.get('recipient') if isinstance(message.get('recipient'), str) else None,
        title=_message_title(role, agent),
        preview=_preview(text),
        text=text,
        status=None,
        phase=None,
        operation_id=None,
        operation=None,
        source_event_ids=[message['id']] if message.get('source', '').startswith('event:') else [],
        truncated_fields=[],
        role=role,
        role_missing=role not in {'user', 'assistant', 'system', 'tool'},
        agent=agent,
        provider_message_id=message.get('provider_message_id'),
        provider_revision=message.get('provider_revision'),
    )


def _activity_entry(task_id, event, agent='', workspace=''):
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    operation = data.get('operation') if isinstance(data.get('operation'), dict) else None
    if operation and (operation.get('path') or operation.get('path_display')):
        meta = relative_path_display(operation.get('path') or operation.get('path_display'), workspace)
        operation = dict(operation, **meta)
        operation = _safe(operation)
    elif operation:
        operation = _safe(operation)
    kind = data.get('kind') if data.get('kind') in {
        'message', 'file_read', 'command', 'test', 'file_change', 'system'} else 'system'
    text = _text(data.get('text') or '')
    title = _text(data.get('title') or {
        'file_read': '读取文件', 'command': '执行命令', 'test': '运行测试',
        'file_change': '文件变更', 'message': '协作消息', 'system': '系统活动',
    }.get(kind, '活动'))
    preview = _text(data.get('preview') or '')
    if not preview:
        if operation and operation.get('command_display'):
            preview = _preview(_text(operation['command_display']))
        elif operation and operation.get('path_display'):
            rng = operation.get('read_range') if isinstance(operation.get('read_range'), dict) else None
            suffix = ''
            if rng and ('start' in rng or 'end' in rng):
                suffix = f" [{rng.get('start', '?')}–{rng.get('end', '?')}]"
            preview = _preview(_text(operation['path_display']) + suffix)
        elif text:
            preview = _preview(text)
        else:
            preview = title
    provenance = data.get('provenance') if data.get('provenance') in {
        'executor_observed', 'controller_reported', 'legacy_summary'} else 'executor_observed'
    operation_id = data.get('operation_id') if isinstance(data.get('operation_id'), str) else None
    # Stable card id from real operation_id so truncated started events do not rename completed cards.
    stable_id = (f'{task_id}:op:{operation_id}' if operation_id
                 else f'{task_id}:activity:{event["seq"]}')
    actor = data.get('actor')
    if isinstance(actor, str) and actor.strip():
        actor = _text(actor)
    elif provenance == 'controller_reported':
        actor = '总控'
    else:
        actor = _text(agent or '')
    recipient = _text(data['recipient']) if isinstance(data.get('recipient'), str) else None
    return dict(
        id=stable_id,
        task_id=task_id,
        kind=kind,
        timestamp=event.get('timestamp'),
        source=_text(data.get('source') or f'event:{event["seq"]}'),
        completeness=data.get('completeness') if data.get('completeness') in {
            'complete', 'recorded', 'truncated', 'possibly_truncated', 'unavailable'} else 'recorded',
        provenance=provenance,
        actor=actor,
        recipient=recipient,
        title=title[:500],
        preview=preview[:500],
        text=text,
        status=data.get('status') if data.get('status') in {'started', 'completed', 'failed', 'unknown'} else None,
        phase=data.get('phase') if data.get('phase') in {'started', 'completed', 'failed', 'progress', 'unknown'} else None,
        operation_id=operation_id,
        operation=operation,
        source_event_ids=[f'event:{event["seq"]}'],
        truncated_fields=list(data.get('truncated_fields') or [])[:40],
        agent=agent,
        event_id=data.get('event_id'),
    )


def _tool_entry(task_id, event, agent='', workspace=''):
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    if isinstance(data.get('operation'), dict) or data.get('activity_kind'):
        # Prefer the paired activity event when both were stored; tool-only fallback below.
        pass
    name = data.get('name') or 'tool'
    status = data.get('status') or 'unknown'
    mapped = {'success': 'completed', 'started': 'started', 'failed': 'failed', 'denied': 'failed'}.get(status, 'unknown')
    operation = data.get('operation') if isinstance(data.get('operation'), dict) else {'tool_name': _text(str(name))[:200]}
    if operation.get('path') or operation.get('path_display'):
        operation = dict(operation, **relative_path_display(
            operation.get('path') or operation.get('path_display'), workspace))
    operation = _safe(operation)
    return dict(
        id=f'{task_id}:tool:{event["seq"]}',
        task_id=task_id,
        kind=data.get('activity_kind') if data.get('activity_kind') in {
            'file_read', 'command', 'test', 'file_change', 'system'} else 'system',
        timestamp=event.get('timestamp'),
        source=f'event:{event["seq"]}',
        completeness=data.get('completeness') if data.get('completeness') == 'unavailable' else 'recorded',
        provenance='executor_observed',
        actor=agent,
        recipient=None,
        title=_text(str(name))[:500],
        preview=_preview(_text(operation.get('command_display') or operation.get('path_display') or str(name))),
        text='',
        status=mapped,
        phase=mapped,
        operation_id=data.get('tool_use_id') if isinstance(data.get('tool_use_id'), str) else None,
        operation=operation,
        source_event_ids=[f'event:{event["seq"]}'],
        truncated_fields=[],
        agent=agent,
    )


def capture_coverage_for(agent, events):
    """Describe what this task's stored events actually contain — never overclaim."""
    kinds = {e['kind'] for e in events}
    has_tool = 'tool' in kinds
    has_message = 'message' in kinds
    activity_events = [e for e in events if e['kind'] == 'activity']
    observed_ops = [e for e in activity_events
                    if (e.get('data') or {}).get('provenance') == 'executor_observed'
                    and (e.get('data') or {}).get('kind') in {
                        'file_read', 'command', 'test', 'file_change', 'system'}]
    controller_ops = [e for e in activity_events
                      if (e.get('data') or {}).get('provenance') == 'controller_reported'
                      and (e.get('data') or {}).get('kind') in {
                          'file_read', 'command', 'test', 'file_change'}]
    controller_messages = [e for e in activity_events
                           if (e.get('data') or {}).get('provenance') == 'controller_reported'
                           and (e.get('data') or {}).get('kind') == 'message']
    agent = agent or 'unknown'
    # Controller-reported messages alone must not look like CLI tool observation.
    if agent in {'codex', 'claude', 'cursor_cli', 'cursor'}:
        if observed_ops or has_tool:
            ops = 'structured_operations'
            note = '已从结构化 CLI 事件提取公开操作字段；未识别类型仅有摘要。'
        else:
            ops = 'messages_only'
            note = '未见结构化工具事件；不表示执行器未操作。'
    elif agent in {'grokbot', 'grok'}:
        ops = 'messages_only'
        note = '仅采集本任务 baseline 之后公开的 assistant send-message；未接入平台未公开的工具参数。'
    elif agent == 'muse':
        ops = 'messages_only'
        note = '仅采集已归属本任务会话的可见回复；不抓跨会话历史或隐藏思考。'
    else:
        if observed_ops or controller_ops:
            ops = 'controller_or_plugin'
            note = '存在上报或插件活动记录；command v1 默认不解析 stdout 为事件。'
        else:
            ops = 'unavailable'
            note = '未接入操作记录；仅有消息或结果摘要时不会伪装工具观测。'
    if controller_ops:
        ops = 'controller_reported' if ops in {'unavailable', 'messages_only'} else ops + '+controller_reported'
        note += ' 含总控上报操作。'
    elif controller_messages and ops == 'messages_only':
        note += ' 含总控上报消息（非 CLI 工具观测）。'
    return dict(
        agent=agent,
        operations=ops,
        messages='recorded' if has_message or activity_events else 'none',
        note=note,
    )


def merge_operation_updates(entries):
    """Collapse started/completed pairs only when operation_id matches within one task."""
    by_key = {}
    order = []
    for entry in entries:
        op_id = entry.get('operation_id')
        if not op_id or entry.get('kind') == 'message':
            order.append(('keep', entry))
            continue
        key = (entry['task_id'], op_id)
        stable_id = f'{entry["task_id"]}:op:{op_id}'
        if key not in by_key:
            entry = dict(entry, id=stable_id)
            by_key[key] = entry
            order.append(('ref', key))
        else:
            current = by_key[key]
            if entry['task_id'] != current['task_id']:
                order.append(('keep', entry))
                continue
            merged = dict(current)
            for field in ('status', 'phase', 'timestamp', 'completeness', 'preview', 'text', 'title', 'operation'):
                if field == 'operation':
                    if entry.get('operation'):
                        merged['operation'] = dict(current.get('operation') or {}, **entry['operation'])
                    continue
                if entry.get(field) not in (None, '', 'started', 'unknown'):
                    merged[field] = entry[field]
            merged['source_event_ids'] = list(dict.fromkeys(
                (current.get('source_event_ids') or []) + (entry.get('source_event_ids') or [])))
            merged['id'] = stable_id
            by_key[key] = merged
    result = []
    for tag, value in order:
        result.append(by_key[value] if tag == 'ref' else value)
    return result


def dedupe_provider_messages(entries):
    """Keep the highest provider_revision for the same provider_message_id within a task."""
    best = {}
    order = []
    for entry in entries:
        pid = entry.get('provider_message_id')
        if not pid or entry.get('kind') != 'message':
            order.append(('keep', entry))
            continue
        key = (entry['task_id'], str(pid))
        revision = entry.get('provider_revision')
        revision = revision if type(revision) is int else -1
        stable = dict(entry, id=f'{entry["task_id"]}:provider:{pid}')
        if key not in best or revision >= best[key][0]:
            best[key] = (revision, stable)
            if not any(tag == 'pref' and val == key for tag, val in order):
                order.append(('pref', key))
    result = []
    for tag, value in order:
        result.append(best[value][1] if tag == 'pref' else value)
    return result


def task_timeline(task, messages, events, *, budget_entries=None, budget_chars=None):
    """Build timeline + capture_coverage for one task.

    messages: dashboard message dicts already sanitized.
    events: bounded raw event dicts (seq/kind/data/timestamp).
    """
    task_id = task['id']
    agent = task.get('agent', '')
    workspace = str(task.get('cwd') or task.get('workspace') or '')
    entries = [_message_entry(message, task_id, agent) for message in messages]
    # Avoid double-counting operations when both tool and activity were stored.
    activity_ids = set()
    for event in events:
        if event['kind'] == 'activity':
            entry = _activity_entry(task_id, event, agent, workspace)
            if entry.get('operation_id'):
                activity_ids.add(entry['operation_id'])
            entries.append(entry)
    for event in events:
        if event['kind'] != 'tool':
            continue
        data = event.get('data') if isinstance(event.get('data'), dict) else {}
        op_id = data.get('tool_use_id') if isinstance(data.get('tool_use_id'), str) else None
        if op_id and op_id in activity_ids:
            continue
        entries.append(_tool_entry(task_id, event, agent, workspace))
    entries = dedupe_provider_messages(entries)
    entries = merge_operation_updates(entries)
    entries.sort(key=lambda item: (
        item.get('timestamp') is None,
        item.get('timestamp') or 0,
        item['task_id'],
        item['id'],
    ))
    limit = MAX_TIMELINE if budget_entries is None else budget_entries
    char_limit = MAX_TEXT if budget_chars is None else budget_chars
    truncated = False
    kept = []
    size = 0
    for entry in entries:
        piece = _entry_size(entry)
        if len(kept) >= limit or size + piece > char_limit:
            truncated = True
            break
        kept.append(entry)
        size += piece
    coverage = capture_coverage_for(agent, events)
    if truncated:
        coverage = dict(coverage, truncated=True)
    return dict(timeline=kept, capture_coverage=coverage, truncated=truncated, text_chars=size)
