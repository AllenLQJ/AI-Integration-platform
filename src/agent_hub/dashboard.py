"""Read-only, bounded browser projections over the existing daemon service.

This module never constructs Service, runs adapters, or claims a controller.
DTOs intentionally exclude task internals and executable/artifact filesystem paths.
"""
import json
import re
from pathlib import Path, PureWindowsPath

from .models import HubError, TERMINAL, redact


_SECRET_KEY = re.compile(r'(?i)(secret|password|credential|authorization|cookie|token|api[_-]?key)')


def _text(value):
    """Redact before slicing, including credentials not covered by legacy Store.safe."""
    value = redact(value if isinstance(value, str) else '')
    value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', value)
    value = re.sub(r'''(?i)(["']?(?:\b[\w-]{0,80}(?:secret|password|token|api[_-]?key|credential))["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s&,;}]+)''',
                   r'\1[REDACTED]', value)
    # Signed links and URL userinfo can contain credentials with arbitrary names.
    value = re.sub(r'''(?i)(?:https?|wss?|ftp)://[^\s<>"']+''', '[URL]', value)
    value = re.sub(r'''(?i)(?<!\w)(?:[a-z]:[\\/]|\\\\)[^\r\n<>"']*''', '[PATH]', value)
    value = re.sub(r'''(?<![\w:/])/(?:[^\s/<>"']+/)*[^\s<>"']*''', '[PATH]', value)
    return value


def _safe(value, depth=0, budget=None):
    """Bound arbitrary custom event payloads; redact keys as well as values."""
    if budget is None:
        budget = [12000, 200]
    budget[1] -= 1
    if depth > 6 or budget[0] <= 0 or budget[1] < 0:
        return '[TRUNCATED]'
    if isinstance(value, str):
        text = _text(value)
        limit = min(8000, budget[0])
        budget[0] -= min(len(text), limit)
        return text[:limit] + (' [TRUNCATED]' if len(text) > limit else '')
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:50]:
            key = str(key)
            safe_key = _text(key)[:100]
            result[safe_key] = '[REDACTED]' if _SECRET_KEY.search(key) else _safe(item, depth + 1, budget)
            if budget[0] <= 0 or budget[1] < 0:
                result['_truncated'] = True
                break
        if len(value) > 50:
            result['_truncated'] = True
        return result
    if isinstance(value, list):
        result = []
        for item in value[:50]:
            result.append(_safe(item, depth + 1, budget))
            if budget[0] <= 0 or budget[1] < 0:
                break
        if len(result) < len(value):
            result.append('[TRUNCATED]')
        return result
    return value if value is None or type(value) in (bool, int, float) else None


def _pick(value, keys):
    return {key: _safe(value[key]) for key in keys if key in value}


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise HubError('INVALID_ARGUMENT', name + ' outside supported range')


def _string(value, name, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (required and not value):
        raise HubError('INVALID_ARGUMENT', name + ' must be a bounded string')


class Dashboard:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.result_files = {}

    def results_unchanged(self):
        return all(self.store.result_file_signature(path) == signature
                   for path, signature in self.result_files.items())

    def _summary(self, task, verification=None):
        fields = self.service.verification.fields(task, summary=verification)
        prompt = _text(task.get('prompt', '')).strip()
        workspace = str(task.get('cwd') or task.get('workspace') or '').rstrip('\\/')
        label = PureWindowsPath(workspace).name
        verification = fields['verification']
        cancel_scope = task.get('cancel_scope')
        if not isinstance(cancel_scope, str):
            adapter = self.service.adapters.get(task.get('agent')) if hasattr(self.service, 'adapters') else None
            cancel_scope = getattr(adapter, 'cancel_scope', None) if adapter is not None else None
        summary = dict(id=task['id'], title=prompt.splitlines()[0][:120] if prompt else task['id'],
                    agent=_text(task.get('agent', '')), state=_text(task['state']),
                    verification_state=_text(verification['state']),
                    verification_label=_text(verification.get('verification_label') or verification['state']),
                    verification_policy=_text(verification.get('verification_policy') or ''),
                    work_status=_text(fields['work_status']),
                    created=task['created'], updated=task['updated'], workspace_label=_text(label)[:120],
                    mode=_text(task.get('mode', '')))
        if isinstance(task.get('delivery'), str):
            summary['delivery'] = _text(task['delivery'])[:64]
        if isinstance(cancel_scope, str) and cancel_scope:
            summary['cancel_scope'] = _text(cancel_scope)[:64]
        if isinstance(task.get('session_id'), str) and task['session_id']:
            summary['has_remote_session'] = True
        if type(task.get('baseline')) is int:
            summary['has_baseline'] = True
        return summary
    def tasks(self, limit=30, offset=0, q='', state='', agent=''):
        _integer(limit, 'limit', 1, 100)
        _integer(offset, 'offset', 0, 9223372036854775807)
        for name, value, maximum in (('q', q, 500), ('state', state, 64), ('agent', agent, 200)):
            _string(value, name, maximum)
        clauses, parameters = [], []
        if q:
            pattern = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            clauses.append("(id LIKE ? ESCAPE '\\' OR json_extract(data, '$.prompt') LIKE ? ESCAPE '\\')")
            parameters.extend([pattern, pattern])
        if state:
            clauses.append('state = ?')
            parameters.append(state)
        if agent:
            clauses.append("json_extract(data, '$.agent') = ?")
            parameters.append(agent)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM tasks' + where + ' ORDER BY created DESC, id DESC LIMIT ? OFFSET ?',
                              (*parameters, limit + 1, offset)).fetchall()
        return dict(items=[self._summary(self.store.decode(row)) for row in rows[:limit]],
                    next_offset=offset + limit if len(rows) > limit else None)

    def agents(self):
        # Capabilities are already loaded. diagnose may expose configuration or probe a process.
        return {'items': [dict(id=_text(name), modes=[_text(mode) for mode in adapter.capabilities
                                                    if isinstance(mode, str)])
                          for name, adapter in sorted(self.service.adapters.items())]}

    def _relations(self, task):
        related = []
        parent_id = task.get('parent_id')
        review_id = (task.get('review_of') or {}).get('task_id') if isinstance(task.get('review_of'), dict) else None
        parent_id = parent_id if isinstance(parent_id, str) else None
        review_id = review_id if isinstance(review_id, str) else None
        with self.store.connection() as db:
            rows = db.execute("""SELECT * FROM tasks WHERE id IN (?, ?)
                OR json_extract(data, '$.parent_id') = ? OR json_extract(data, '$.review_of.task_id') = ?
                ORDER BY created, id LIMIT 200""", (parent_id, review_id, task['id'], task['id'])).fetchall()
        for row in rows:
            other = self.store.decode(row)
            kinds = []
            if other['id'] == parent_id:
                kinds.append('continued_from' if task.get('resume_session') else 'switched_from')
            if other.get('parent_id') == task['id']:
                kinds.append('continuation' if other.get('resume_session') else 'switch_executor')
            if other['id'] == review_id:
                kinds.append('review_of')
            if isinstance(other.get('review_of'), dict) and other['review_of'].get('task_id') == task['id']:
                kinds.append('review')
            related.extend(dict(task_id=other['id'], kind=kind, agent=_text(other.get('agent', '')),
                                state=_text(other['state'])) for kind in kinds)
        return related

    @staticmethod
    def _message(ident, role, text, source, completeness, timestamp):
        safe = _text(text)
        if len(safe) > 100000:
            safe = safe[:100000]
            completeness = 'truncated'
        return dict(id=ident, role=role, text=safe, source=source, completeness=completeness, timestamp=timestamp)

    def chain_details(self, task_ids):
        """Stream bounded projections from one request-local DB snapshot.

        Each event subquery uses the task/seq index and its own LIMIT, so a busy
        task cannot exhaust another task's history or load unbounded history.
        Relations, criteria and artifacts belong to the separately polled node
        inspector and are not needed by the chain's conversation projection.
        """
        ids = list(dict.fromkeys(task_ids))
        if len(ids) > 200:
            raise HubError('INVALID_ARGUMENT', 'Too many displayed nodes')
        with self.store.connection() as db:
            db.execute('BEGIN')
            for offset in range(0, len(ids), 25):
                group = ids[offset:offset + 25]
                batch = dict(tasks={}, verification={}, messages={}, timeline={})
                placeholders = ','.join('?' for _ in group)
                for row in db.execute(f'SELECT * FROM tasks WHERE id IN ({placeholders})', group):
                    batch['tasks'][row['id']] = self.store.decode(row)
                for row in db.execute(f'SELECT * FROM verifications WHERE task_id IN ({placeholders})', group):
                    batch['verification'][row['task_id']] = dict(row)
                queries, parameters = [], []
                for ident in group:
                    # A sentinel also projects nodes with no events. UNION ALL
                    # streams one node at a time; do not fetchall these payloads.
                    queries.append("SELECT 'node' AS bucket, NULL AS seq, ? AS task_id, "
                                   'NULL AS kind, NULL AS data, NULL AS timestamp')
                    parameters.append(ident)
                    for bucket, condition, limit in (
                        ('messages', "kind='message'", 201),
                        ('timeline', "kind IN ('message','activity','tool','progress','approval','system')", 401)):
                        queries.append(f"SELECT '{bucket}' AS bucket, * FROM (SELECT * FROM events "
                                       f"WHERE task_id=? AND {condition} ORDER BY seq DESC LIMIT {limit})")
                        parameters.append(ident)
                current = None
                for row in db.execute(' UNION ALL '.join(queries), parameters):
                    if row['bucket'] == 'node':
                        if current is not None:
                            yield current, self.detail(current, _batch=batch)
                        batch['messages'].clear()
                        batch['timeline'].clear()
                        current = row['task_id']
                    else:
                        batch[row['bucket']].setdefault(row['task_id'], []).append(dict(row))
                if current is not None:
                    yield current, self.detail(current, _batch=batch)

    def detail(self, task_id, *, _batch=None):
        _string(task_id, 'task_id', 200, required=True)
        if _batch is None:
            task = self.store.get(task_id)
            summary = self.service.verification.summary(task)
        else:
            task = _batch['tasks'].get(task_id)
            if task is None:
                raise HubError('TASK_NOT_FOUND', 'Task not found')
            summary = self.service.verification.summary_from_row(task, _batch['verification'].get(task_id))
        messages = [self._message(task_id + ':prompt', 'user', task.get('prompt', ''),
                                  'task.prompt', 'complete', task['created'])]
        if _batch is None:
            with self.store.connection() as db:
                rows = db.execute("SELECT * FROM events WHERE task_id=? AND kind='message' ORDER BY seq DESC LIMIT 201",
                                  (task_id,)).fetchall()
        else:
            rows = sorted(_batch['messages'].get(task_id, []), key=lambda row: row['seq'], reverse=True)
        recorded = []
        for row in reversed(rows[:200]):
            data = json.loads(row['data'])
            if not isinstance(data, dict):
                continue
            text = data.get('message', data.get('text'))
            if not isinstance(text, str) or not text:
                continue
            completeness = 'truncated' if data.get('truncated') is True else (
                'possibly_truncated' if len(text) >= 1000 else 'recorded')
            role = data.get('role')
            # Keep missing role as None — UI titles use agent fallback; never invent assistant.
            if not (isinstance(role, str) and role in {'user', 'assistant', 'system', 'tool'}):
                role = None
            if role in {'user', None} and text == task.get('prompt'):
                continue
            message = self._message(task_id + ':event:' + str(row['seq']), role, text,
                                    'event:' + str(row['seq']), completeness, row['timestamp'])
            if isinstance(data.get('provider_message_id'), str):
                message['provider_message_id'] = data['provider_message_id'][:200]
            if type(data.get('provider_revision')) is int:
                message['provider_revision'] = data['provider_revision']
            if isinstance(data.get('actor'), str) and data['actor'].strip():
                message['actor'] = _text(data['actor'])[:200]
            if isinstance(data.get('recipient'), str) and data['recipient'].strip():
                message['recipient'] = _text(data['recipient'])[:200]
            recorded.append(message)
        result = {}
        if task['state'] in TERMINAL:
            reference = task.get('result')
            path = reference.get('path') if isinstance(reference, dict) else None
            if isinstance(path, str) and path:
                self.result_files.setdefault(path, self.store.result_file_signature(path))
            if _batch is None:
                result = self.service.call('get_result', {'task_id': task_id})
            else:
                result = self.store.read_result(task)
        answer = result.get('answer')
        final = self._message(task_id + ':result', 'assistant', answer, 'task.result', 'complete', task['updated']) if isinstance(answer, str) and answer else None
        if final:
            # CLI adapters can aggregate several event messages into one final answer.
            joined = '\n'.join(m['text'] for m in recorded)
            if joined and joined == final['text'] and all(m['role'] in {'assistant', None} for m in recorded):
                recorded = []
            else:
                recorded = [m for m in recorded if m['role'] not in {'assistant', None} or not (
                    m['text'] == final['text'] or (
                        m['completeness'] in {'truncated', 'possibly_truncated'} and final['text'].startswith(m['text'])))]
        if len(rows) > 200:
            messages.append(self._message(task_id + ':history-limit', 'system',
                'Only the latest 200 recorded messages are included; earlier events remain available in the event timeline.',
                'dashboard', 'truncated', None))
        messages.extend(recorded)
        if final:
            messages.append(final)
        if isinstance(result.get('error'), str) and result['error']:
            messages.append(self._message(task_id + ':error', 'system', result['error'], 'task.result.error', 'complete', task['updated']))
        if result.get('result_available') is False:
            messages.append(self._message(task_id + ':result-unavailable', 'system',
                result['result_error'], 'task.result.unavailable', 'complete', task['updated']))
        acceptance = task.get('acceptance') or {}
        criteria = [_pick(item, ('id', 'kind', 'required', 'description', 'path', 'expected'))
                    for item in acceptance.get('criteria', [])[:100] if isinstance(item, dict)]
        check_rows = []
        for check in (summary.get('checks') or [])[:100]:
            if not isinstance(check, dict):
                continue
            row = _pick(check, (
                'criterion_id', 'outcome', 'reason', 'evidence', 'verifier_id', 'recorded_at', 'checked_at'))
            verifier = check.get('verifier')
            if isinstance(verifier, dict) and isinstance(verifier.get('name'), str):
                row['verifier_id'] = _text(verifier['name'])[:200]
            check_rows.append(row)
        artifacts = ([_pick(item, ('artifact_id', 'logical_id', 'sha256', 'size_bytes', 'mime_type', 'source', 'change'))
                      for item in self.service.verification.artifacts(task_id)[:200] if isinstance(item, dict)]
                     if _batch is None else [])
        from .dashboard_timeline import task_timeline
        if _batch is None:
            timeline_page = self.store.timeline_events(task_id, limit=400)
        else:
            timeline_rows = sorted(_batch['timeline'].get(task_id, []), key=lambda row: row['seq'], reverse=True)
            timeline_page = dict(truncated=len(timeline_rows) > 400, events=[
                dict(seq=row['seq'], kind=row['kind'], data=json.loads(row['data']), timestamp=row['timestamp'])
                for row in reversed(timeline_rows[:400])])
        projected = task_timeline(task, messages, timeline_page['events'])
        if timeline_page['truncated']:
            projected['capture_coverage'] = dict(
                projected['capture_coverage'],
                note=projected['capture_coverage']['note'] + ' 事件查询已达上限。')
            projected['truncated'] = True
        return dict(task=self._summary(task, verification=summary), messages=messages,
                    relations=self._relations(task) if _batch is None else [],
                    criteria=criteria, verification_checks=check_rows, artifacts=artifacts,
                    acceptance_policy=_text(acceptance.get('policy') or ''),
                    timeline=projected['timeline'],
                    capture_coverage=projected['capture_coverage'])

    def events(self, task_id, after_seq=0, before_seq=None):
        _string(task_id, 'task_id', 200, required=True)
        _integer(after_seq, 'after_seq', 0, 9223372036854775807)
        if before_seq is not None:
            _integer(before_seq, 'before_seq', 1, 9223372036854775807)
            if after_seq != 0:
                raise HubError('INVALID_ARGUMENT', '不能同时读取两个方向的事件')
            page = self.store.events_before(task_id, before_seq, limit=200)
        else:
            page = self.store.events(task_id, after_seq, limit=200)
        return dict(events=[dict(seq=item['seq'], kind=_text(item['kind'])[:200], data=_safe(item['data']),
                                 timestamp=item['timestamp']) for item in page['events']], next_seq=page['next_seq'],
                    **({'has_more': page['has_more']} if before_seq is not None else {}))
