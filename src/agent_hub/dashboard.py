"""Read-only, bounded browser projections over the existing daemon service.

This module never constructs Service, runs adapters, or claims a controller.
DTOs intentionally exclude task internals and executable/artifact filesystem paths.
"""
import json
import re
from pathlib import PureWindowsPath

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

    def _summary(self, task):
        fields = self.service.verification.fields(task)
        prompt = _text(task.get('prompt', '')).strip()
        workspace = str(task.get('cwd') or task.get('workspace') or '').rstrip('\\/')
        label = PureWindowsPath(workspace).name
        return dict(id=task['id'], title=prompt.splitlines()[0][:120] if prompt else task['id'],
                    agent=_text(task.get('agent', '')), state=_text(task['state']),
                    verification_state=_text(fields['verification']['state']), work_status=_text(fields['work_status']),
                    created=task['created'], updated=task['updated'], workspace_label=_text(label)[:120],
                    mode=_text(task.get('mode', '')))

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

    def detail(self, task_id):
        _string(task_id, 'task_id', 200, required=True)
        task = self.store.get(task_id)
        messages = [self._message(task_id + ':prompt', 'user', task.get('prompt', ''),
                                  'task.prompt', 'complete', task['created'])]
        with self.store.connection() as db:
            rows = db.execute("SELECT * FROM events WHERE task_id=? AND kind='message' ORDER BY seq DESC LIMIT 201",
                              (task_id,)).fetchall()
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
            role = role if isinstance(role, str) and role in {'user', 'assistant', 'system', 'tool'} else 'unknown'
            if role in {'user', 'unknown'} and text == task.get('prompt'):
                continue
            recorded.append(self._message(task_id + ':event:' + str(row['seq']), role, text,
                                          'event:' + str(row['seq']), completeness, row['timestamp']))
        result = self.service.call('get_result', {'task_id': task_id}) if task['state'] in TERMINAL else {}
        answer = result.get('answer')
        final = self._message(task_id + ':result', 'assistant', answer, 'task.result', 'complete', task['updated']) if isinstance(answer, str) and answer else None
        if final:
            # CLI adapters can aggregate several event messages into one final answer.
            joined = '\n'.join(m['text'] for m in recorded)
            if joined and joined == final['text'] and all(m['role'] in {'assistant', 'unknown'} for m in recorded):
                recorded = []
            else:
                recorded = [m for m in recorded if m['role'] not in {'assistant', 'unknown'} or not (
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
        acceptance = task.get('acceptance') or {}
        criteria = [_pick(item, ('id', 'kind', 'required', 'description', 'path', 'expected'))
                    for item in acceptance.get('criteria', [])[:100] if isinstance(item, dict)]
        artifacts = [_pick(item, ('artifact_id', 'logical_id', 'sha256', 'size_bytes', 'mime_type', 'source', 'change'))
                     for item in self.service.verification.artifacts(task_id)[:200] if isinstance(item, dict)]
        return dict(task=self._summary(task), messages=messages, relations=self._relations(task),
                    criteria=criteria, artifacts=artifacts)

    def events(self, task_id, after_seq=0):
        _string(task_id, 'task_id', 200, required=True)
        _integer(after_seq, 'after_seq', 0, 9223372036854775807)
        page = self.store.events(task_id, after_seq, limit=200)
        return dict(events=[dict(seq=item['seq'], kind=_text(item['kind'])[:200], data=_safe(item['data']),
                                 timestamp=item['timestamp']) for item in page['events']], next_seq=page['next_seq'])
