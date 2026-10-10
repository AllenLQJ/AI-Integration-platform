import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .models import HubError, TERMINAL, redact


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
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, timestamp REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        data = json.loads(row['data'])
        data.update(id=row['id'], request_id=row['request_id'], state=row['state'], created=row['created'], updated=row['updated'])
        return data

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
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)',
                       (ident, request_id, digest, 'queued', serialized, now, now))
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
            rows = db.execute('SELECT * FROM tasks ORDER BY created').fetchall()
            return [self.decode(r) for r in rows if states is None or r['state'] in states]

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
            db.execute('UPDATE tasks SET state=?, data=?, updated=? WHERE id=?',
                       (state, json.dumps(data, ensure_ascii=False), time.time(), task_id))
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
            db.execute('UPDATE tasks SET state=?, data=?, updated=? WHERE id=?',
                       (state, json.dumps(data, ensure_ascii=False), now, task_id))
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task_id, 'state', json.dumps({'state': state}), now))
            return True

    def wait_snapshot(self, task_id, after_seq):
        with self.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise HubError('TASK_NOT_FOUND', '任务不存在')
            rows = db.execute('SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 201',
                              (task_id, after_seq)).fetchall()
            events = [dict(seq=r['seq'], kind=r['kind'], data=json.loads(r['data']),
                           timestamp=r['timestamp']) for r in rows[:200]]
            return {'state': row['state'], 'events': events,
                    'next_seq': events[-1]['seq'] if events else after_seq,
                    'has_more': len(rows) > 200}

    def event(self, task_id, kind, data):
        with self.connection() as db:
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task_id, kind, json.dumps(self.safe(data), ensure_ascii=False), time.time()))

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

    def recover(self):
        for task in self.list({'running', 'cancelling'}):
            self.update(task['id'], workspace_quarantined=True)
            self.finish(task['id'], 'interrupted', {'error': '服务重启，执行状态无法确认；未自动重发'})
