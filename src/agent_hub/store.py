import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from .models import HubError, TERMINAL, redact


class _PooledConnection:
    __slots__ = ('db', '__weakref__')

    def __init__(self, db):
        self.db = db


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    digest TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    workspace_quarantined INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, timestamp REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
                CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
                CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(json_extract(data, '$.parent_id'));
                CREATE INDEX IF NOT EXISTS tasks_review ON tasks(json_extract(data, '$.review_of.task_id'));
                CREATE TABLE IF NOT EXISTS activity_receipts (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    task_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL, response TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS activity_receipts_task ON activity_receipts(task_id);
                CREATE TABLE IF NOT EXISTS reconcile_requests (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    task_id TEXT NOT NULL, response TEXT NOT NULL);
            ''')
            cols = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            if 'workspace_quarantined' not in cols:
                db.execute('ALTER TABLE tasks ADD COLUMN workspace_quarantined INTEGER NOT NULL DEFAULT 0')
                db.execute(
                    "UPDATE tasks SET workspace_quarantined = 1 "
                    "WHERE json_extract(data, '$.workspace_quarantined') = 1")
            db.execute(
                'CREATE INDEX IF NOT EXISTS tasks_quarantined ON tasks(workspace_quarantined) '
                'WHERE workspace_quarantined = 1')

    def reuse_connections(self):
        """Keep one connection per thread for this Store; close() releases them.

        Opening a connection costs hundreds of microseconds, which dominates
        small reads. Nested use within a thread still opens a separate
        connection so each `with connection()` remains its own transaction.
        """
        self._local = threading.local()
        # Holders, not connections: sqlite3.Connection has no weakref slot. A
        # finished thread drops its holder, which closes that connection.
        self._cached = weakref.WeakSet()
        self._cached_lock = threading.Lock()

    def close(self):
        if getattr(self, '_local', None) is None:
            return
        with self._cached_lock:
            cached = list(self._cached)
            self._cached.clear()
        self._local = None
        for holder in cached:
            try:
                holder.db.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def connection(self):
        local = getattr(self, '_local', None)
        cached = local is not None and not getattr(local, 'busy', False)
        holder = getattr(local, 'holder', None) if cached else None
        if holder is not None:
            db = holder.db
        else:
            # Cached connections are only used by their owning thread; close()
            # may run elsewhere, hence check_same_thread=False.
            db = sqlite3.connect(self.path, timeout=30, check_same_thread=not cached)
            db.row_factory = sqlite3.Row
            if cached:
                holder = local.holder = _PooledConnection(db)
                with self._cached_lock:
                    self._cached.add(holder)
        if cached:
            local.busy = True
        broken = False
        try:
            with db:
                yield db
        except sqlite3.Error:
            broken = True
            raise
        finally:
            if not cached:
                db.close()
            else:
                local.busy = False
                if broken or db.in_transaction:
                    # Never hand a connection with an unknown transaction state to the next caller.
                    local.holder = None
                    with self._cached_lock:
                        self._cached.discard(holder)
                    try:
                        db.close()
                    except sqlite3.Error:
                        pass

    @staticmethod
    def decode(row):
        data = json.loads(row['data'])
        data.update(id=row['id'], request_id=row['request_id'], state=row['state'], created=row['created'], updated=row['updated'])
        return data

    @staticmethod
    def result_file_signature(path):
        try:
            stat = Path(path).stat()
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def read_result(task):
        reference = task.get('result') or {}
        if not isinstance(reference, dict):
            reference = {}
        if not reference.get('path'):
            return dict(reference)
        try:
            result = json.loads(Path(reference['path']).read_text(encoding='utf-8'))
            if not isinstance(result, dict):
                raise ValueError('result must be an object')
            return result
        except (OSError, ValueError, TypeError):
            # Preserve inline uncertainty evidence, never disclose the local path
            # or turn a previously recorded execution into a fabricated failure.
            evidence = {key: value for key, value in reference.items() if key != 'path'}
            return dict(evidence, result_available=False, result_error_code='RESULT_UNAVAILABLE',
                        result_error='执行结果文件不可用，请检查数据目录或备份。')

    def create(self, request_id, spec):
        serialized = json.dumps(spec, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同内容')
                return self.decode(row)
            ident, now = uuid.uuid4().hex, time.time()
            quarantined = 1 if spec.get('workspace_quarantined') else 0
            db.execute(
                'INSERT INTO tasks(id, request_id, digest, state, data, created, updated, '
                'workspace_quarantined) VALUES (?,?,?,?,?,?,?,?)',
                (ident, request_id, digest, 'queued', serialized, now, now, quarantined))
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (ident, 'state', '{"state":"queued"}', now))
            return self.decode(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            return self.decode(row)

    def by_request(self, request_id):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE request_id=?', (request_id,)).fetchone()
            return self.decode(row) if row else None

    def list(self, states=None):
        with self.connection() as db:
            if states is None:
                rows = db.execute('SELECT * FROM tasks ORDER BY created').fetchall()
            else:
                wanted = tuple(states)
                if not wanted:
                    return []
                placeholders = ','.join('?' * len(wanted))
                rows = db.execute(
                    f'SELECT * FROM tasks WHERE state IN ({placeholders}) ORDER BY created',
                    wanted).fetchall()
            return [self.decode(r) for r in rows]

    def list_with_acceptance(self, states):
        """Tasks in states that carry acceptance criteria, filtered in SQL instead of decoding all rows."""
        wanted = tuple(states)
        if not wanted:
            return []
        placeholders = ','.join('?' * len(wanted))
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM tasks WHERE state IN ({placeholders}) "
                "AND json_type(data, '$.acceptance') = 'object' ORDER BY created", wanted).fetchall()
            return [self.decode(r) for r in rows]

    def list_quarantined(self):
        """Tasks with workspace_quarantined=1 via indexed column (JSON field kept in data)."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE workspace_quarantined = 1 ORDER BY created"
            ).fetchall()
            return [self.decode(r) for r in rows]

    def update(self, task_id, **fields):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            if row['state'] in TERMINAL:
                return False
            data = json.loads(row['data'])
            state = fields.pop('state', row['state'])
            data.update(fields)
            quarantined = 1 if data.get('workspace_quarantined') else 0
            db.execute(
                'UPDATE tasks SET state=?, data=?, updated=?, workspace_quarantined=? WHERE id=?',
                (state, json.dumps(data, ensure_ascii=False), time.time(), quarantined, task_id))
            return True

    def finish(self, task_id, state, result):
        if state not in TERMINAL:
            raise ValueError('not terminal')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            if row['state'] in TERMINAL:
                return False
            data = json.loads(row['data'])
            data['result'] = result
            now = time.time()
            quarantined = 1 if data.get('workspace_quarantined') else 0
            db.execute(
                'UPDATE tasks SET state=?, data=?, updated=?, workspace_quarantined=? WHERE id=?',
                (state, json.dumps(data, ensure_ascii=False), now, quarantined, task_id))
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task_id, 'state', json.dumps({'state': state}), now))
            return True

    def wait_snapshot(self, task_id, after_seq):
        with self.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT state, data FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            rows = db.execute('SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 201',
                              (task_id, after_seq)).fetchall()
            events = [dict(seq=r['seq'], kind=r['kind'], data=json.loads(r['data']),
                           timestamp=r['timestamp']) for r in rows[:200]]
            return {'state': row['state'], 'intervention': json.loads(row['data']).get('intervention'), 'events': events,
                    'next_seq': events[-1]['seq'] if events else after_seq,
                    'has_more': len(rows) > 200}

    def event(self, task_id, kind, data):
        with self.connection() as db:
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task_id, kind, json.dumps(self.safe(data), ensure_ascii=False), time.time()))

    def record_activity(self, request_id, task_id, event_id, activity):
        """Atomically append kind=activity with an independent idempotent receipt.

        Does not mutate task state, updated timestamp, or verification rows.
        """
        from .activity import activity_digest
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        if not isinstance(event_id, str) or not 0 < len(event_id) <= 200 or '\x00' in event_id:
            raise HubError('INVALID_ARGUMENT', 'event_id 需要非空字符串，最多 200 字符')
        if not isinstance(activity, dict):
            raise HubError('INVALID_ARGUMENT', 'activity 需要对象')
        digest = activity_digest(event_id, activity)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            receipt = db.execute('SELECT * FROM activity_receipts WHERE request_id=?', (request_id,)).fetchone()
            if receipt:
                if receipt['digest'] != digest or receipt['task_id'] != task_id or receipt['event_id'] != event_id:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同活动内容')
                return json.loads(receipt['response'])
            now = time.time()
            payload = dict(activity, event_id=event_id)
            cursor = db.execute(
                'INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                (task_id, 'activity', json.dumps(self.safe(payload), ensure_ascii=False), now))
            seq = cursor.lastrowid
            response = dict(ok=True, task_id=task_id, event_id=event_id, seq=seq,
                            request_id=request_id, kind='activity')
            db.execute(
                'INSERT INTO activity_receipts(request_id,digest,task_id,event_id,event_seq,response) '
                'VALUES (?,?,?,?,?,?)',
                (request_id, digest, task_id, event_id, seq, json.dumps(response, ensure_ascii=False)))
            return response

    def timeline_events(self, task_id, limit=400):
        """Bounded newest-first retrieval of timeline-relevant events for one task."""
        self.get(task_id)
        limit = max(1, min(int(limit), 400))
        kinds = ('message', 'activity', 'tool', 'progress', 'approval', 'system')
        placeholders = ','.join('?' * len(kinds))
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM events WHERE task_id=? AND kind IN ({placeholders}) "
                f"ORDER BY seq DESC LIMIT ?",
                (task_id, *kinds, limit + 1)).fetchall()
        truncated = len(rows) > limit
        events = [dict(seq=r['seq'], kind=r['kind'], data=json.loads(r['data']),
                       timestamp=r['timestamp']) for r in reversed(rows[:limit])]
        return {'events': events, 'truncated': truncated}

    @staticmethod
    def reconcile_audit_summary(observation):
        """Bounded audit fields — never store full answers in task JSON history."""
        evidence = observation.get('completion_evidence') if isinstance(observation.get('completion_evidence'), dict) else {}
        progress = observation.get('progress') if isinstance(observation.get('progress'), dict) else {}
        return {
            'status': observation.get('status'),
            'remote_observation': observation.get('remote_observation'),
            'error_code': observation.get('error_code'),
            'session_id': observation.get('session_id'),
            'baseline': observation.get('baseline'),
            'own_message_observed': observation.get('own_message_observed'),
            'own_marker_seen': observation.get('own_marker_seen'),
            'reply_seq': evidence.get('reply_seq') or progress.get('reply_seq'),
            'idle_polls': evidence.get('idle_polls') if evidence.get('idle_polls') is not None
            else progress.get('idle_count'),
            'marker': evidence.get('marker') or progress.get('marker'),
            'cursor': progress.get('cursor'),
            'observed_at': observation.get('observed_at'),
        }

    @staticmethod
    def reconcile_progress_payload(observation, task_data):
        """Safe cross-request resume fields; only for the same session/baseline/marker."""
        progress = observation.get('progress') if isinstance(observation.get('progress'), dict) else {}
        evidence = observation.get('completion_evidence') if isinstance(
            observation.get('completion_evidence'), dict) else {}
        sid = observation.get('session_id') or task_data.get('session_id')
        baseline = observation.get('baseline') if observation.get('baseline') is not None else task_data.get('baseline')
        marker = evidence.get('marker') or progress.get('marker')
        cursor = progress.get('cursor')
        idle = progress.get('idle_count')
        if idle is None:
            idle = evidence.get('idle_polls')
        reply_seq = evidence.get('reply_seq') or progress.get('reply_seq')
        answer = progress.get('answer')
        if not isinstance(answer, str) and isinstance(observation.get('answer'), str):
            answer = observation['answer']
        if not (isinstance(sid, str) and type(baseline) is int and isinstance(marker, str)
                and isinstance(cursor, str) and re.fullmatch(r'-?\d+', cursor)
                and type(idle) is int and 0 <= idle <= 10000):
            return None
        payload = dict(session_id=sid, baseline=baseline, marker=marker, cursor=cursor,
                       idle_count=idle,
                       own_message_observed=bool(observation.get('own_message_observed')
                                                 or progress.get('own_message_observed')
                                                 or evidence.get('own_message_observed')),
                       own_marker_seen=bool(observation.get('own_marker_seen')
                                                             or progress.get('own_marker_seen')
                                                             or evidence.get('reply_seq') is not None))
        if type(reply_seq) is int:
            payload['reply_seq'] = reply_seq
        if isinstance(answer, str) and answer and answer.rstrip().endswith(marker):
            # Bound stored answer so task JSON does not balloon across retries.
            payload['answer'] = answer if len(answer) <= 100000 else answer[:100000]
        return payload

    def apply_reconcile(self, request_id, task_id, observation, *, allow_complete=False, result_path=None,
                        expected_session=None, expected_baseline=None):
        """Constrained reconcile persistence — never a generic terminal overwrite.

        Completing requires verified evidence (session/baseline/reply_seq/marker/idle),
        not merely status=completed. Cancelled/failed/succeeded/new attempts untouched.
        """
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        if not isinstance(observation, dict):
            raise HubError('INVALID_ARGUMENT', 'observation 需要对象')
        digest = hashlib.sha256(json.dumps(
            dict(tool='reconcile_task', task_id=task_id),
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt = db.execute('SELECT * FROM reconcile_requests WHERE request_id=?',
                                 (request_id,)).fetchone()
            if receipt:
                if receipt['digest'] != digest or receipt['task_id'] != task_id:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同核对内容')
                return json.loads(receipt['response'])
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            state = row['state']
            data = json.loads(row['data'])
            now = time.time()
            summary = self.safe(dict(self.reconcile_audit_summary(observation), recorded_at=now))
            data['remote_observation'] = summary.get('remote_observation') or summary.get('status')
            data['last_reconcile'] = summary
            history = data.get('reconcile_history')
            if not isinstance(history, list):
                history = []
            history = (history + [summary])[-20:]
            data['reconcile_history'] = history
            # Persist resume progress for incomplete probes; clear on terminal complete.
            progress = self.reconcile_progress_payload(observation, data)
            if progress and observation.get('status') != 'completed':
                data['reconcile_progress'] = self.safe(progress)
            completed = False
            new_state = state
            evidence = observation.get('completion_evidence') if isinstance(
                observation.get('completion_evidence'), dict) else {}
            marker = '[HUB-END:' + task_id + ']'
            # Result file must already exist and be readable before we reference it.
            path_ok = False
            if isinstance(result_path, str) and result_path:
                try:
                    path_ok = Path(result_path).is_file() and bool(
                        Path(result_path).read_text(encoding='utf-8'))
                except (OSError, UnicodeError):
                    path_ok = False
            evidence_ok = (
                observation.get('status') == 'completed'
                and isinstance(observation.get('answer'), str) and observation['answer']
                and observation.get('session_id') == (expected_session or data.get('session_id'))
                and observation.get('baseline') == (expected_baseline if expected_baseline is not None
                                                    else data.get('baseline'))
                and type(evidence.get('reply_seq')) is int
                and evidence.get('marker') == marker
                and isinstance(observation.get('answer'), str)
                and observation['answer'].rstrip().endswith(marker)
                and type(evidence.get('idle_polls')) is int and evidence['idle_polls'] >= 4
                and path_ok
            )
            if (allow_complete and evidence_ok and state in {'interrupted', 'delivery_unknown'}
                    and isinstance(result_path, str) and result_path):
                data['result'] = {'path': result_path, 'reconciled': True}
                data['reconciled_from_state'] = state
                data.pop('reconcile_progress', None)
                new_state = 'succeeded'
                completed = True
                db.execute(
                    'UPDATE tasks SET state=?, data=?, updated=?, workspace_quarantined=? WHERE id=?',
                    (new_state, json.dumps(data, ensure_ascii=False), now,
                     1 if data.get('workspace_quarantined') else 0, task_id))
                db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                           (task_id, 'state', json.dumps({'state': new_state, 'via': 'reconcile'}), now))
            else:
                db.execute(
                    'UPDATE tasks SET data=?, updated=? WHERE id=?',
                    (json.dumps(data, ensure_ascii=False), now, task_id))
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task_id, 'reconcile', json.dumps(summary, ensure_ascii=False), now))
            response = dict(
                ok=True, task_id=task_id, request_id=request_id, state=new_state,
                completed=completed, remote_observation=data.get('remote_observation'),
                observation=summary, prior_state=state)
            db.execute(
                'INSERT INTO reconcile_requests(request_id,digest,task_id,response) VALUES (?,?,?,?)',
                (request_id, digest, task_id, json.dumps(response, ensure_ascii=False)))
            return response

    @staticmethod
    def safe(value):
        if isinstance(value, dict):
            import re
            return {k: '[REDACTED]' if re.fullmatch(
                r'(?i)(authorization|cookie|api[_-]?key|access_token|auth_token|token|password|secret)', k)
                else Store.safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [Store.safe(v) for v in value]
        return redact(value) if isinstance(value, str) else value

    def events(self, task_id, after_seq=0, limit=50):
        self.get(task_id)
        limit = max(1, min(int(limit), 200))
        with self.connection() as db:
            rows = db.execute('SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?',
                              (task_id, int(after_seq), limit)).fetchall()
            events = [dict(seq=r['seq'], kind=r['kind'], data=json.loads(r['data']),
                           timestamp=r['timestamp']) for r in rows]
            return {'events': events, 'next_seq': events[-1]['seq'] if events else after_seq}

    def events_before(self, task_id, before_seq, limit=200):
        self.get(task_id)
        limit = max(1, min(int(limit), 200))
        with self.connection() as db:
            rows = db.execute('SELECT * FROM events WHERE task_id=? AND seq<? ORDER BY seq DESC LIMIT ?',
                              (task_id, before_seq, limit + 1)).fetchall()
        events = [dict(seq=r['seq'], kind=r['kind'], data=json.loads(r['data']), timestamp=r['timestamp'])
                  for r in reversed(rows[:limit])]
        return dict(events=events, next_seq=events[-1]['seq'] if events else 0, has_more=len(rows) > limit)

    def recover(self):
        for task in self.list({'running', 'waiting_for_user', 'cancelling'}):
            self.update(task['id'], workspace_quarantined=True)
            self.finish(task['id'], 'interrupted', {'error': '服务重启，执行状态无法确认；未自动重发'})
