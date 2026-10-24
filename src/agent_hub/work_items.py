"""Stable collaboration work-item containers, independent of verification attempts."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from .models import HubError

RELATIONS = frozenset({'root', 'development', 'review', 'rework', 'continuation', 'switch', 'linked'})
REVIEW_LIKE = frozenset({'review', 'rework'})
DEFAULT_TITLE = '工作项'


class WorkItems:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_item_members (
                    work_item_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                    relation TEXT NOT NULL, related_task_id TEXT,
                    linked_at REAL NOT NULL, link_request_id TEXT,
                    PRIMARY KEY (work_item_id, task_id));
                CREATE INDEX IF NOT EXISTS work_item_members_item ON work_item_members(work_item_id);
                CREATE TABLE IF NOT EXISTS work_item_requests (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL);
            ''')

    def _request(self, request_id, content, operation):
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM work_item_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同工作项内容')
                return json.loads(row['response'])
            response = operation(db)
            db.execute('INSERT INTO work_item_requests(request_id,digest,response) VALUES (?,?,?)',
                       (request_id, digest, json.dumps(response, ensure_ascii=False)))
            return response

    def create(self, request_id, title=DEFAULT_TITLE, acceptance=None):
        if not isinstance(title, str) or not 0 < len(title) <= 500 or '\x00' in title:
            raise HubError('INVALID_ARGUMENT', 'title 需要非空字符串，最多 500 字符')
        from .verification import validate
        validate(acceptance)
        payload = {}
        if acceptance is not None:
            payload['acceptance'] = acceptance

        def apply(db):
            now = time.time()
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO work_items(id,title,created,updated,data) VALUES (?,?,?,?,?)',
                       (ident, title, now, now, json.dumps(payload, ensure_ascii=False)))
            return dict(work_item_id=ident, title=title, created=now, updated=now,
                        acceptance=acceptance)

        return self._request(request_id, dict(tool='create_work_item', title=title, acceptance=acceptance), apply)

    def get(self, work_item_id):
        if not isinstance(work_item_id, str) or not work_item_id:
            raise HubError('INVALID_ARGUMENT', 'work_item_id 需要非空字符串')
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM work_items WHERE id=?', (work_item_id,)).fetchone()
        if not row:
            raise HubError('WORK_ITEM_NOT_FOUND', '工作项不存在')
        data = json.loads(row['data']) if row['data'] else {}
        return dict(id=row['id'], title=row['title'], created=row['created'], updated=row['updated'],
                    data=data, acceptance=data.get('acceptance'))

    def members(self, work_item_id):
        self.get(work_item_id)
        with self.store.connection() as db:
            rows = db.execute(
                'SELECT * FROM work_item_members WHERE work_item_id=? ORDER BY linked_at, task_id',
                (work_item_id,)).fetchall()
        return [dict(row) for row in rows]

    def membership(self, task_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM work_item_members WHERE task_id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    def _membership_db(self, db, task_id):
        row = db.execute('SELECT * FROM work_item_members WHERE task_id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    def require_for_submit(self, work_item_id, relation, related_task_id):
        if not isinstance(work_item_id, str) or not work_item_id:
            raise HubError('WORK_ITEM_REQUIRED',
                           '公开提交必须提供 work_item_id；请先 create_work_item，或用 link_task 关联历史任务')
        item = self.get(work_item_id)
        relation = relation or 'development'
        if relation not in RELATIONS:
            raise HubError('INVALID_ARGUMENT', 'relation 无效')
        if relation in REVIEW_LIKE:
            if not isinstance(related_task_id, str) or not related_task_id:
                raise HubError('INVALID_ARGUMENT', '审核/返修必须提供同一工作项内的 related_task_id')
            member = self.membership(related_task_id)
            if not member or member['work_item_id'] != work_item_id:
                raise HubError('INVALID_ARGUMENT', 'related_task_id 必须已属于同一 work_item_id')
        elif related_task_id is not None:
            if not isinstance(related_task_id, str) or not related_task_id:
                raise HubError('INVALID_ARGUMENT', 'related_task_id 无效')
            member = self.membership(related_task_id)
            if not member or member['work_item_id'] != work_item_id:
                raise HubError('INVALID_ARGUMENT', 'related_task_id 必须已属于同一 work_item_id')
        return item, relation

    def _would_cycle_db(self, db, work_item_id, task_id, related_task_id):
        """True if adding task→related creates a cycle via related_task_id edges."""
        if not related_task_id:
            return False
        seen = set()
        current = related_task_id
        while current:
            if current == task_id:
                return True
            if current in seen:
                return True
            seen.add(current)
            row = db.execute(
                'SELECT related_task_id FROM work_item_members WHERE work_item_id=? AND task_id=?',
                (work_item_id, current)).fetchone()
            current = row['related_task_id'] if row and row['related_task_id'] else None
        return False

    def _attach_db(self, db, work_item_id, task_id, relation, related_task_id=None, request_id=None,
                   *, audit=False):
        """Validate and insert membership inside an open IMMEDIATE transaction. No nested connections."""
        if relation not in RELATIONS:
            raise HubError('INVALID_ARGUMENT', 'relation 无效')
        item = db.execute('SELECT id FROM work_items WHERE id=?', (work_item_id,)).fetchone()
        if not item:
            raise HubError('WORK_ITEM_NOT_FOUND', '工作项不存在')
        task_row = db.execute('SELECT id FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task_row:
            raise HubError('TASK_NOT_FOUND', '任务不存在')
        # Existing members: idempotent/conflict before cycle checks (historical rings must not
        # block same-content replay or mislabel relation conflicts as cycle errors).
        existing = self._membership_db(db, task_id)
        if existing:
            if existing['work_item_id'] != work_item_id:
                raise HubError('INVALID_ARGUMENT', '任务已属于另一工作项')
            if (existing['relation'] != relation
                    or (existing['related_task_id'] or None) != (related_task_id or None)):
                raise HubError('REQUEST_CONFLICT', '任务已关联且 relation/related_task_id 与请求不一致')
            return dict(work_item_id=work_item_id, task_id=task_id, relation=existing['relation'],
                        related_task_id=existing['related_task_id'], already_linked=True)
        if related_task_id is not None:
            if not isinstance(related_task_id, str) or not related_task_id:
                raise HubError('INVALID_ARGUMENT', 'related_task_id 无效')
            if related_task_id == task_id:
                raise HubError('INVALID_ARGUMENT', '禁止自环关联')
            related_row = db.execute('SELECT id FROM tasks WHERE id=?', (related_task_id,)).fetchone()
            if not related_row:
                raise HubError('TASK_NOT_FOUND', 'related_task_id 不存在')
            related_member = self._membership_db(db, related_task_id)
            if not related_member or related_member['work_item_id'] != work_item_id:
                raise HubError('INVALID_ARGUMENT', 'related_task_id 必须已属于同一 work_item_id')
            if self._would_cycle_db(db, work_item_id, task_id, related_task_id):
                raise HubError('INVALID_ARGUMENT', '禁止循环关联')
        now = time.time()
        db.execute(
            'INSERT INTO work_item_members(work_item_id,task_id,relation,related_task_id,linked_at,link_request_id) '
            'VALUES (?,?,?,?,?,?)',
            (work_item_id, task_id, relation, related_task_id, now, request_id))
        db.execute('UPDATE work_items SET updated=? WHERE id=?', (now, work_item_id))
        if audit:
            db.execute(
                'INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                (task_id, 'work_item_link',
                 json.dumps(self.store.safe(dict(
                     work_item_id=work_item_id, relation=relation, related_task_id=related_task_id,
                     request_id=request_id)), ensure_ascii=False),
                 now))
        return dict(work_item_id=work_item_id, task_id=task_id, relation=relation,
                    related_task_id=related_task_id, already_linked=False)

    def attach(self, work_item_id, task_id, relation, related_task_id=None, request_id=None):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._attach_db(db, work_item_id, task_id, relation, related_task_id, request_id)

    def link_task(self, request_id, work_item_id, task_id, relation='linked', related_task_id=None):
        """Explicit historical association — metadata only; fully atomic with receipt."""
        if relation not in RELATIONS:
            raise HubError('INVALID_ARGUMENT', 'relation 无效')
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        content = dict(tool='link_task', work_item_id=work_item_id, task_id=task_id,
                       relation=relation, related_task_id=related_task_id)
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM work_item_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同工作项内容')
                return json.loads(row['response'])
            response = self._attach_db(
                db, work_item_id, task_id, relation, related_task_id, request_id, audit=True)
            db.execute('INSERT INTO work_item_requests(request_id,digest,response) VALUES (?,?,?)',
                       (request_id, digest, json.dumps(response, ensure_ascii=False)))
            return response

    def inherit_from_parent(self, parent_task):
        member = self.membership(parent_task['id'])
        if member:
            return member['work_item_id'], 'continuation' if parent_task.get('resume_session') else 'switch', parent_task['id']
        wid = parent_task.get('work_item_id')
        if isinstance(wid, str) and wid:
            return wid, 'continuation' if parent_task.get('resume_session') else 'switch', parent_task['id']
        return None, None, None

    def acceptance_for_dispatch(self, work_item_id, relation):
        """Development/rework/continuation inherit work-item acceptance; review tasks do not."""
        if relation in REVIEW_LIKE:
            return None
        item = self.get(work_item_id)
        return item.get('acceptance')

    @staticmethod
    def explicit_review_target(task):
        """Compatible projection for declared independent_reviewer context only."""
        context = task.get('context') if isinstance(task.get('context'), dict) else {}
        payload = context.get('payload') if isinstance(context.get('payload'), dict) else {}
        if payload.get('role') != 'independent_reviewer':
            return None
        developer = payload.get('developer_task_id')
        return developer if isinstance(developer, str) and developer else None

    def project_declared_review_pair(self, request_id, developer_task_id, review_task_id,
                                     title=DEFAULT_TITLE):
        """Explicit migration for a declared independent_reviewer pair — metadata only."""
        developer = self.store.get(developer_task_id)
        review = self.store.get(review_task_id)
        target = self.explicit_review_target(review)
        if target != developer_task_id:
            raise HubError('INVALID_ARGUMENT',
                           '仅支持 context.payload.role=independent_reviewer 且 developer_task_id 已声明的审核任务')
        content = dict(tool='project_declared_review_pair', developer_task_id=developer_task_id,
                       review_task_id=review_task_id, title=title)
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM work_item_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同工作项内容')
                return json.loads(row['response'])
            now = time.time()
            wid = uuid.uuid4().hex
            db.execute('INSERT INTO work_items(id,title,created,updated,data) VALUES (?,?,?,?,?)',
                       (wid, title, now, now, '{}'))
            self._attach_db(db, wid, developer_task_id, 'development', None, request_id + ':dev')
            self._attach_db(db, wid, review_task_id, 'review', developer_task_id, request_id + ':rev')
            response = dict(work_item_id=wid, title=title, members=[
                dict(task_id=developer_task_id, relation='development'),
                dict(task_id=review_task_id, relation='review', related_task_id=developer_task_id),
            ])
            db.execute('INSERT INTO work_item_requests(request_id,digest,response) VALUES (?,?,?)',
                       (request_id, digest, json.dumps(response, ensure_ascii=False)))
            return response

    def membership_map(self):
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM work_item_members').fetchall()
        return {row['task_id']: dict(row) for row in rows}

    def items_index(self):
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM work_items').fetchall()
        return {row['id']: dict(id=row['id'], title=row['title'], created=row['created'],
                                updated=row['updated']) for row in rows}


def software_dashboard_acceptance():
    """Template: tests + UI + independent Grok review as required content_review criteria."""
    return {
        'policy': 'software_dashboard',
        'criteria': [
            {'id': 'tests', 'kind': 'content_review', 'required': True,
             'description': '自动化测试通过（总控登记产物证据）'},
            {'id': 'ui', 'kind': 'content_review', 'required': True,
             'description': '页面功能验证通过（总控登记产物证据）'},
            {'id': 'review', 'kind': 'content_review', 'required': True,
             'description': 'Grok 独立审核通过（总控登记产物证据）'},
        ],
    }


def conversation_acceptance():
    """Explicit no-extra verification policy for ordinary chat."""
    return {'policy': 'no_extra'}
