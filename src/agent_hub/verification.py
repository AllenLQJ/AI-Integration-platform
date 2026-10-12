"""独立于执行状态的第一阶段验收。"""
import hashlib
import json
import os
import stat
import re
import threading
import time
import uuid
import weakref
from pathlib import Path, PureWindowsPath
from .models import HubError


def safe(value):
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items() if k.lower() not in {'url', 'transfer'}}
    if isinstance(value, list):
        return [safe(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s/<>]+@[^\s<>]+', '[URL]', value)
        return re.sub(r'https?://[^\s<>"\']+', '[URL]', value)
    return value


def relative_path(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096 and '\x00' not in value
            and bool(Path(value).parts) and not Path(value).is_absolute() and not PureWindowsPath(value).anchor
            and '..' not in value.replace('\\', '/').split('/'))


def validate(acceptance):
    if acceptance is None:
        return
    if (not isinstance(acceptance, dict) or set(acceptance) != {'criteria'}
            or not isinstance(acceptance['criteria'], list) or not 1 <= len(acceptance['criteria']) <= 100):
        raise HubError('INVALID_ARGUMENT', 'acceptance.criteria 需要 1–100 条检查')
    ids = set()
    for c in acceptance['criteria']:
        if not isinstance(c, dict):
            raise HubError('INVALID_ARGUMENT', '验收条件需要对象')
        kind = c.get('kind')
        fields = {'artifact_required': set(), 'file_exists': {'path'},
                  'sha256': {'path', 'expected'}, 'content_review': {'description'}}
        if not isinstance(kind, str) or kind not in fields or set(c) - (fields[kind] | {'id', 'kind', 'required'}):
            raise HubError('INVALID_ARGUMENT', '未知检查类型或参数')
        ident = c.get('id')
        if not isinstance(ident, str) or not ident or len(ident) > 200 or ident in ids:
            raise HubError('INVALID_ARGUMENT', '检查 id 需要唯一非空字符串，最多 200 字符')
        ids.add(ident)
        if type(c.get('required', True)) is not bool:
            raise HubError('INVALID_ARGUMENT', 'required 需要布尔值')
        if kind in {'file_exists', 'sha256'} and not relative_path(c.get('path')):
            raise HubError('INVALID_ARGUMENT', 'path 需要工作区内相对路径，禁止绝对路径和 ..')
        if 'expected' in c and (not isinstance(c['expected'], str) or not re.fullmatch('[0-9a-fA-F]{64}', c['expected'])):
            raise HubError('INVALID_ARGUMENT', 'expected 需要 64 位 SHA-256')
        if kind == 'content_review' and (not isinstance(c.get('description'), str) or not 0 < len(c['description']) <= 10000):
            raise HubError('INVALID_ARGUMENT', 'description 需要非空字符串，最多 10000 字符')


class VerificationStopped(HubError):
    def __init__(self):
        super().__init__('VERIFICATION_STOPPED', '验收已停止，重启后重新检查')


class Verification:
    def __init__(self, store, gateway, stopping=None):
        self.store, self.gateway = store, gateway
        self.lock = threading.RLock()
        self.task_locks, self.request_locks = weakref.WeakValueDictionary(), weakref.WeakValueDictionary()
        self.stopping = stopping or threading.Event()
        self.local = threading.local()
        self.max_files, self.max_total_bytes, self.max_entries = 1000, 256 * 1024 * 1024, 5000
        self.max_scan_seconds = 10
        self.pending_ids = set()
        self.max_file_bytes = 64 * 1024 * 1024
        self.max_retries = 3
        self.use_dir_fd = os.name == 'posix'
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS controllers (root_task_id TEXT PRIMARY KEY, controller_id TEXT NOT NULL, epoch INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS verifications (
                    task_id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS verification_inputs (
                    task_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS verification_checks (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_index (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS verification_requests (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT);
                CREATE INDEX IF NOT EXISTS verification_state ON verifications(state);
                CREATE INDEX IF NOT EXISTS verification_check_task ON verification_checks(task_id);
                CREATE INDEX IF NOT EXISTS artifact_task ON artifact_index(task_id);
                UPDATE verifications SET state='awaiting_verification' WHERE state='verifying';
            ''')
        # 补齐历史任务及 finish 后尚未入队就退出的窗口。
        for task in store.list():
            self.ensure(task)
            self.track(task)

    def track(self, task):
        if task.get('acceptance'):
            with self.lock:
                self.pending_ids.add(task['id'])

    def checkpoint(self):
        cancel = getattr(self.local, 'cancel', None)
        if self.stopping.is_set() or cancel is not None and cancel.is_set():
            raise VerificationStopped()
        budget = getattr(self.local, 'budget', None)
        if budget and (budget['files'] > self.max_files or budget['bytes'] > self.max_total_bytes or budget['entries'] > self.max_entries):
            raise HubError('VERIFICATION_UNCERTAIN', '文件扫描超过数量或总字节预算，请人工核对')
        if budget and time.monotonic() - budget['started'] > self.max_scan_seconds:
            raise HubError('VERIFICATION_UNCERTAIN', '文件扫描超过时间预算，请人工核对')

    def charge(self, key, amount, maximum):
        self.checkpoint()
        budget = getattr(self.local, 'budget', None)
        if budget is not None:
            budget[key] += amount
            if budget[key] > maximum:
                raise HubError('VERIFICATION_UNCERTAIN', '文件扫描超过数量或总字节预算，请人工核对')

    def task_lock(self, task_id):
        with self.lock:
            return self.task_locks.setdefault(task_id, threading.RLock())

    def ensure(self, task):
        state = 'awaiting_verification' if task['state'] == 'succeeded' and task.get('acceptance') else 'not_requested'
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO verifications VALUES (?,?,?)', (task['id'], state, '{}'))
            if state == 'awaiting_verification':
                db.execute("UPDATE verifications SET state=? WHERE task_id=? AND state='not_requested'", (state, task['id']))

    def summary(self, task, infer=True):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM verifications WHERE task_id=?', (task['id'],)).fetchone()
        if not row:
            return dict(state='awaiting_verification' if task['state'] == 'succeeded' and task.get('acceptance') else 'not_requested')
        state = row['state']
        if infer and state == 'not_requested' and task['state'] == 'succeeded' and task.get('acceptance'):
            state = 'awaiting_verification'
        return dict(json.loads(row['data']), state=state)

    def fields(self, task):
        v = self.summary(task)
        state = task['state']
        work = state if state != 'succeeded' or v['state'] == 'not_requested' else (
            'completed' if v['state'] == 'passed' else v['state'])
        return dict(verification=v, work_status=work)

    def history(self, task_id):
        with self.store.connection() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM verification_checks WHERE task_id=? ORDER BY seq', (task_id,))]

    def artifacts(self, task_id):
        with self.store.connection() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM artifact_index WHERE task_id=? ORDER BY id', (task_id,))]

    def index(self, task, source, key, data):
        ident = uuid.uuid5(uuid.NAMESPACE_URL, task['id'] + ':' + source + ':' + key + ':' + data['sha256']).hex
        item = dict(data, artifact_id=ident, logical_id='artifact://' + ident, source=source)
        item.setdefault('change', 'reused')
        with self.store.connection() as db:
            old = db.execute('SELECT data FROM artifact_index WHERE id=?', (ident,)).fetchone()
            if old:
                item = dict(json.loads(old['data']), **item)
            db.execute('INSERT INTO artifact_index VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data', (ident, task['id'], json.dumps(item)))
        if not old:
            self.store.event(task['id'], 'artifact', safe(self.store.safe(item)))
        return item

    def read_file(self, root, name, limit=None):
        self.checkpoint()
        self.charge("files", 1, self.max_files)
        if not relative_path(name):
            raise HubError('INVALID_ARGUMENT', '文件路径越界')
        root = Path(root).resolve()
        path = root / name
        cursor = root
        for part in Path(name).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise HubError('INVALID_ARGUMENT', '符号链接路径不允许作为验收产物')
        if not path.resolve().is_relative_to(root):
            raise HubError('INVALID_ARGUMENT', '符号链接逃逸工作区')
        digest, size = hashlib.sha256(), 0
        try:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise HubError('ARTIFACT_NOT_FOUND', '不是普通文件：' + name)
            if limit is not None and before.st_size > limit:
                raise HubError('VERIFICATION_UNCERTAIN', '文件超过验收哈希上限，请人工核对：' + name)
            if self.use_dir_fd:
                # 逐级以目录描述符定位；拒绝检查期间被替换的符号链接。
                directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    parts = Path(name).parts
                    for part in parts[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                        os.close(directory); directory = child
                    descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                finally:
                    os.close(directory)
                f = os.fdopen(descriptor, 'rb')
            else:
                f = path.open('rb')
            with f:
                opened = os.fstat(f.fileno())
                if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                        or not path.resolve().is_relative_to(root)):
                    raise HubError('VERIFICATION_UNCERTAIN', '打开期间文件身份或真实路径改变，请人工核对')
                if not stat.S_ISREG(opened.st_mode):
                    raise HubError('ARTIFACT_NOT_FOUND', '不是普通文件：' + name)
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    self.charge("bytes", len(chunk), self.max_total_bytes)
                    digest.update(chunk); size += len(chunk)
                    if limit is not None and size > limit:
                        raise HubError('VERIFICATION_UNCERTAIN', '文件超过验收哈希上限，请人工核对')
                after = path.stat()
                if (not path.resolve().is_relative_to(root) or
                        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                    raise HubError('VERIFICATION_UNCERTAIN', '读取期间文件改变，请人工核对')
        except FileNotFoundError:
            raise HubError('ARTIFACT_NOT_FOUND', '文件不存在：' + name) from None
        return dict(sha256=digest.hexdigest(), size_bytes=size, identity=[opened.st_dev, opened.st_ino])

    def file(self, task, name, actual=None, change=None):
        actual = dict(actual) if actual is not None else self.read_file(task.get('workspace') or task['cwd'], name)
        if change is None:
            with self.store.connection() as db:
                row = db.execute('SELECT data FROM verification_inputs WHERE task_id=?', (task['id'],)).fetchone()
            baseline = json.loads(row['data']) if row else {}
            try:
                values = {('workspace', k): v for k, v in baseline.get('files', {}).items()}
                values.update({('media', k): v for k, v in baseline.get('media', {}).items()})
                change = self.change(baseline.get('files', {}).get(name), actual, values) if row and not baseline.get('error') else 'reused'
            except HubError:
                change = 'reused'
        actual.pop('identity', None)
        claimed = {}
        result = task.get('result', {})
        if result.get('path'):
            result = json.loads(Path(result['path']).read_text(encoding='utf-8'))
        values = result.get('artifacts', {})
        for key in ('sha256', 'size_bytes'):
            value = values.get(key)
            if type(value) in (int, float) or isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{64}', value):
                claimed[key] = value
        return self.index(task, 'workspace', name, dict(actual, path=name, claimed=claimed, change=change))

    def workspace_files(self, task):
        root = Path(task.get('workspace') or task['cwd']).resolve()
        data_root = Path(self.store.path).parent.resolve()
        owned_dirs = {'logs', 'results', 'media-artifacts', 'worktrees'}
        if any(root.is_relative_to(data_root / name) for name in {'logs', 'results'}):
            return
        directories = [root]
        while directories:
            directory = directories.pop()
            self.checkpoint()
            with os.scandir(directory) as entries:
                for entry in entries:
                    self.charge('entries', 1, self.max_entries)
                    name, path = entry.name, Path(entry.path)
                    if name == '.git' or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if (path == data_root and not root.is_relative_to(data_root) or
                                directory == data_root and name in owned_dirs):
                            continue
                        directories.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        if directory == data_root and (name.startswith('tasks.db') or
                                name in {'config.json', 'connection.json', 'connection.tmp', 'daemon.log', 'daemon.lock'}):
                            continue
                        yield str(path.relative_to(root))

    def snapshot_files(self, task):
        result = {}
        for name in self.workspace_files(task):
            try:
                result[name] = self.read_file(task.get('workspace') or task['cwd'], name, self.max_file_bytes)
            except VerificationStopped:
                raise
            except Exception as error:
                result[name] = {'error': type(error).__name__}
                try:
                    info = (Path(task.get('workspace') or task['cwd']) / name).stat()
                    result[name]['identity'] = [info.st_dev, info.st_ino]
                except OSError:
                    pass
        return result

    def snapshot(self, task, cancel=None):
        if not task.get('acceptance') or not any(c['kind'] == 'artifact_required' for c in task['acceptance']['criteria']):
            return
        self.local.cancel = cancel
        self.local.budget = dict(files=0, bytes=0, entries=0, started=time.monotonic())
        try:
            files = self.snapshot_files(task)
            root = self.gateway.artifact_root.resolve()
            media, owners = {}, []
            with self.store.connection() as db:
                # 登记行不重新哈希，不占读取文件额度；逐行检查时间/停止预算。
                for row in db.execute('SELECT data FROM media_artifacts'):
                    self.checkpoint()
                    data = json.loads(row['data']); path = Path(data['path'])
                    try:
                        info = path.stat()
                        owners.append([info.st_dev, info.st_ino])
                        media[str(path.relative_to(root))] = dict(sha256=data['sha256'], size_bytes=data['size_bytes'], identity=[info.st_dev, info.st_ino])
                    except FileNotFoundError:
                        pass
            if root.exists():
                for name in self.workspace_files(dict(cwd=str(root))):
                    if name not in media:
                        media[name] = self.read_file(root, name, self.max_file_bytes)
            data = dict(version=2, files=files, media=media, media_owners=owners)
            if any(item.get('error') for item in files.values()):
                data['error'] = '执行前基线不完整'
        except VerificationStopped:
            raise
        except Exception as error:
            data = dict(version=2, error=str(error))
        finally:
            self.local.budget = None
            self.local.cancel = None
        self.checkpoint()
        with self.store.connection() as db:
            db.execute('INSERT OR IGNORE INTO verification_inputs VALUES (?,?)', (task['id'], json.dumps(data)))

    @staticmethod
    def change(before, actual, baseline):
        if before is None:
            # 改名/硬链接仍是执行前的同一文件，不能仅凭新路径视为 created。
            before = next((item for item in baseline.values() if isinstance(item, dict)
                           and item.get('identity') == actual.get('identity')), None)
        if before is None:
            return 'copied' if any(item.get('sha256') == actual['sha256'] for item in baseline.values()) else 'created'
        if not isinstance(before, dict) or before.get('error') or 'sha256' not in before:
            raise HubError('VERIFICATION_UNCERTAIN', '执行前文件哈希未知，请人工核对')
        if before['sha256'] == actual['sha256']:
            return 'reused'
        return 'copied' if any(item.get('sha256') == actual['sha256'] for item in baseline.values()) else 'changed'

    def media(self, task, entries, registered):
        if not task.get('acceptance'):
            return
        with self.store.connection() as db:
            row = db.execute('SELECT data FROM verification_inputs WHERE task_id=?', (task['id'],)).fetchone()
        baseline = json.loads(row['data']) if row else {}
        for entry, actual in zip(entries, registered):
            claimed = {k: entry[k] for k in ('sha256', 'size_bytes', 'width', 'height')
                       if k in entry and (type(entry[k]) in (int, float) or
                                          isinstance(entry[k], str) and len(entry[k]) <= 64 and ':' not in entry[k])}
            self.index(task, 'media', actual['artifact_id'], dict(
                media_artifact_id=actual['artifact_id'], sha256=actual['sha256'],
                size_bytes=actual['size_bytes'], claimed=self.store.safe(claimed), change=self.media_change(dict(actual, path=entry['path']), baseline)))

    def media_change(self, actual, snapshot):
        try:
            name = str(Path(actual['path']).relative_to(self.gateway.artifact_root.resolve()))
            return self.change(snapshot.get('media', {}).get(name), actual,
                               {**{('workspace', k): v for k, v in snapshot.get('files', {}).items()}, **{('media', k): v for k, v in snapshot.get('media', {}).items()}})
        except (HubError, KeyError, ValueError):
            return 'reused'

    def owned_media(self, task_id, path):
        with self.store.connection() as db:
            rows = db.execute('SELECT data FROM media_artifacts WHERE task_id<>?', (task_id,)).fetchall()
        for row in rows:
            self.checkpoint()
            other = Path(json.loads(row['data'])['path'])
            try:
                if other == path or other.samefile(path):
                    return True
            except FileNotFoundError:
                pass
        return False

    def check(self, task, criterion):
        kind = criterion['kind']
        if kind == 'content_review':
            return 'inconclusive', '没有配置审核端，请人工审核：' + criterion['description'], {}
        if kind in {'file_exists', 'sha256'}:
            evidence = self.file(task, criterion['path'])
            if kind == 'sha256' and criterion.get('expected') and evidence['sha256'] != criterion['expected'].lower():
                return 'fail', '文件 SHA-256 不匹配，请修正产物或验收条件', evidence
            return 'pass', 'Hub 已读取并检查文件', evidence
        with self.store.connection() as db:
            row = db.execute('SELECT data FROM verification_inputs WHERE task_id=?', (task['id'],)).fetchone()
            media_rows = db.execute('SELECT * FROM media_artifacts').fetchall()
        snapshot = json.loads(row['data']) if row else {'error': '没有执行前文件快照'}
        if snapshot.get('version') != 2:
            return 'inconclusive', '执行前快照缺少内容哈希或文件身份，请人工核对', {}
        if snapshot.get('error'):
            return 'inconclusive', '无法核对执行前文件，请人工核对：' + snapshot['error'], {}
        baseline = {('workspace', k): v for k, v in snapshot['files'].items()}
        baseline.update({('media', k): v for k, v in snapshot['media'].items()})
        uncertain = []
        for row in media_rows:
            if row['task_id'] != task['id']:
                continue
            registered = json.loads(row['data'])
            path = Path(registered['path'])
            # 已被其他任务登记的文件，即使内容改变也不算本次产物。
            try:
                info = path.stat()
                reused = [info.st_dev, info.st_ino] in snapshot.get('media_owners', [])
                for other in media_rows:
                    if other['task_id'] == task['id']:
                        continue
                    old_path = Path(json.loads(other['data'])['path'])
                    if old_path == path:
                        reused = True; break
                    try:
                        if old_path.samefile(path):
                            reused = True; break
                    except FileNotFoundError:
                        pass
                if reused:
                    self.index(task, 'media', row['id'], dict(media_artifact_id=row['id'], sha256=registered['sha256'], size_bytes=registered['size_bytes'], change='reused'))
                    continue
                name = str(path.relative_to(self.gateway.artifact_root.resolve()))
                actual = self.read_file(self.gateway.artifact_root, name, self.max_file_bytes)
                if 'media' not in snapshot:
                    raise HubError('VERIFICATION_UNCERTAIN', '没有执行前媒体快照，请人工核对')
                change = self.change(snapshot['media'].get(name), actual, baseline)
                item = self.index(task, 'media', row['id'], dict(media_artifact_id=row['id'], sha256=actual['sha256'], size_bytes=actual['size_bytes'], change=change))
                if self.owned_media(task['id'], path):
                    self.index(task, 'media', row['id'], dict(item, change='reused'))
                    continue
                if change in {'created', 'changed'} or change == 'copied' and self.expected_artifact(task, actual['sha256'], name):
                    data = registered
                    if data['sha256'] != actual['sha256']:
                        raise HubError('VERIFICATION_UNCERTAIN', '媒体在检查期间改变，请人工核对')
                    item = self.index(task, 'media', row['id'], dict(
                        media_artifact_id=row['id'], sha256=data['sha256'], size_bytes=data['size_bytes'], change=change))
                    return 'pass', 'Hub 已检查本次媒体产物', {'artifacts': [dict(item, change=change)]}
            except VerificationStopped:
                raise
            except Exception as error:
                uncertain.append(str(error))
        for name in self.workspace_files(task):
            # 运行数据内的媒体目录不能通过工作区扫描绕过媒体归属规则。
            path = Path(task.get('workspace') or task['cwd']) / name
            if path.resolve().is_relative_to(self.gateway.artifact_root.resolve()):
                continue
            try:
                actual = self.read_file(task.get('workspace') or task['cwd'], name, self.max_file_bytes)
                change = self.change(snapshot['files'].get(name), actual, baseline)
                if change in {'created', 'changed'} or change == 'copied' and self.expected_artifact(task, actual['sha256'], name):
                    item = self.file(task, name, actual, change)
                    if item['sha256'] != actual['sha256']:
                        raise HubError('VERIFICATION_UNCERTAIN', '产物在检查期间改变，请人工核对')
                    return 'pass', 'Hub 已检查本次工作区产物', {'artifacts': [dict(item, change=change)]}
            except VerificationStopped:
                raise
            except Exception as error:
                uncertain.append(str(error))
        if uncertain:
            return 'inconclusive', '无法确定产物内容变化，请人工核对：' + '; '.join(uncertain[:3]), {}
        return 'fail', '没有本次新增或内容改变的产物；请生成媒体附件或写入文件，声明路径或 touch 不能替代产物', {}

    @staticmethod
    def decision(criteria, checks):
        required = {c['id'] for c in criteria if c.get('required', True)}
        outcomes = [c['outcome'] for c in checks if c['criterion_id'] in required]
        return 'rework_required' if 'fail' in outcomes else 'needs_review' if 'inconclusive' in outcomes else 'passed'

    def save(self, task_id, state, checks, run_id, request_id=None, history=None, extra=None, expected_run=None):
        self.checkpoint()
        data = dict(checks=checks, verification_run_id=run_id, checked_at=time.time())
        response = dict(verification=dict(safe(self.store.safe(data)), state=state),
                        work_status='completed' if state == 'passed' else state, **(extra or {}))
        with self.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM verifications WHERE task_id=?', (task_id,)).fetchone()
            if expected_run and (not row or row['state'] != 'verifying' or
                                 json.loads(row['data']).get('verification_run_id') != expected_run):
                raise HubError('INVALID_STATE', '本次验收已被替代，结果未写入')
            for check in checks if history is None else history:
                db.execute('INSERT INTO verification_checks(task_id,data) VALUES (?,?)', (task_id, json.dumps(safe(self.store.safe(check)))))
            db.execute('UPDATE verifications SET state=?,data=? WHERE task_id=?', (state, json.dumps(safe(self.store.safe(data))), task_id))
            if request_id:
                db.execute('UPDATE verification_requests SET response=? WHERE request_id=?', (json.dumps(response), request_id))
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task_id, 'verification', json.dumps(safe(self.store.safe(dict(state=state, verification_run_id=run_id, checks=checks)))), time.time()))
            self.checkpoint()
        with self.lock:
            self.pending_ids.discard(task_id)
        return response

    def run(self, task_id, request_id=None, automatic=False):
        with self.task_lock(task_id):
            task = self.store.get(task_id)
            if task['state'] != 'succeeded' or not task.get('acceptance'):
                raise HubError('INVALID_STATE', '只有成功且请求验收的任务可以重验')
            current = self.summary(task, infer=False)
            run_id = uuid.uuid4().hex
            try:
                self.checkpoint()
                self.ensure(task)
                if automatic and self.summary(task)['state'] != 'awaiting_verification':
                    with self.lock:
                        self.pending_ids.discard(task_id)
                    return self.fields(task)
                with self.lock, self.store.connection() as db:
                    data = dict(current, verification_run_id=run_id)
                    data.pop('state', None)
                    db.execute("UPDATE verifications SET state='verifying',data=? WHERE task_id=?", (json.dumps(data), task_id))
                checks = []
                self.local.budget = dict(files=0, bytes=0, entries=0, started=time.monotonic())
                for c in task['acceptance']['criteria']:
                    try:
                        self.checkpoint()
                        outcome, reason, evidence = self.check(task, c)
                    except VerificationStopped:
                        raise
                    except HubError as error:
                        outcome = 'fail' if c['kind'] in {'file_exists', 'sha256'} and error.code in {'INVALID_ARGUMENT', 'ARTIFACT_NOT_FOUND'} else 'inconclusive'
                        reason, evidence = str(error), {}
                    except Exception as error:
                        outcome, reason, evidence = 'inconclusive', '检查异常，请人工核对：' + type(error).__name__, {}
                    checks.append(dict(criterion_id=c['id'], outcome=outcome, reason=reason, evidence=evidence,
                                       verifier={'name': 'hub-deterministic', 'version': '1'}, checked_at=time.time(), verification_run_id=run_id))
                self.local.budget = None
                with self.lock:
                    return self.save(task_id, self.decision(task['acceptance']['criteria'], checks), checks, run_id,
                                     request_id, expected_run=run_id)
            except VerificationStopped:
                with self.lock, self.store.connection() as db:
                    row = db.execute('SELECT * FROM verifications WHERE task_id=?', (task_id,)).fetchone()
                    if row and row['state'] == 'verifying' and json.loads(row['data']).get('verification_run_id') == run_id:
                        db.execute("UPDATE verifications SET state='awaiting_verification',data='{}' WHERE task_id=?", (task_id,))
                raise
            except Exception as error:
                self.retry(task, run_id, error, current)
                raise
            finally:
                self.local.budget = None

    def retry(self, task, run_id, error, previous):
        self.track(task)
        # 独立落盘，不依赖可能失败的 save；超限绝不接受已有 pass 结果。
        with self.lock, self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM verifications WHERE task_id=?', (task['id'],)).fetchone()
            data = json.loads(row['data']) if row else {}
            own_run = row and row['state'] == 'verifying' and data.get('verification_run_id') == run_id
            before_start = not row or (row['state'] == previous['state'] and
                                      data.get('verification_run_id') == previous.get('verification_run_id'))
            if not own_run and not before_start:
                return
            if not row:
                db.execute('INSERT INTO verifications VALUES (?,?,?)', (task['id'], 'awaiting_verification', '{}'))
            retries = data.get('retry_count', 0) + 1
            state = 'needs_review' if retries >= self.max_retries else 'awaiting_verification'
            reason = '验收落盘或状态切换失败，请人工核对：' + type(error).__name__
            checks = [dict(criterion_id=c['id'], outcome='inconclusive', reason=reason, evidence={},
                           verifier={'name': 'hub-recovery', 'version': '1'}, checked_at=time.time(),
                           verification_run_id=run_id) for c in task['acceptance']['criteria']]
            data = dict(retry_count=retries, next_retry_at=time.time() + .2 * 2 ** (retries - 1),
                        reason=reason, checks=checks, verification_run_id=run_id)
            db.execute('UPDATE verifications SET state=?,data=? WHERE task_id=?', (state, json.dumps(data), task['id']))
            if state == 'needs_review':
                for check in checks:
                    db.execute('INSERT INTO verification_checks(task_id,data) VALUES (?,?)', (task['id'], json.dumps(check)))
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task['id'], 'verification_retry', json.dumps(dict(state=state, reason=reason, retry_count=retries)), time.time()))

    def pending(self):
        with self.lock:
            ids = list(self.pending_ids)
        candidates = []
        for ident in ids:
            task = self.store.get(ident)
            if task['state'] == 'succeeded':
                summary = self.summary(task)
                if summary['state'] == 'awaiting_verification' and summary.get('next_retry_at', 0) <= time.time():
                    candidates.append(ident)
                elif summary['state'] not in {'awaiting_verification', 'verifying'}:
                    with self.lock:
                        self.pending_ids.discard(ident)
            elif task['state'] in {'failed', 'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
                with self.lock:
                    self.pending_ids.discard(ident)
        return candidates

    def request(self, request_id, content, operation):
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        with self.lock:
            request_lock = self.request_locks.setdefault(request_id, threading.RLock())
        with request_lock:
            with self.store.connection() as db:
                row = db.execute('SELECT * FROM verification_requests WHERE request_id=?', (request_id,)).fetchone()
                if row and row['digest'] != digest:
                    raise HubError('IDEMPOTENCY_CONFLICT', '验收请求内容发生变化')
                if row and row['response']:
                    return json.loads(row['response'])
                db.execute('INSERT OR IGNORE INTO verification_requests VALUES (?,?,NULL)', (request_id, digest))
            try:
                response = operation()
            except Exception:
                with self.store.connection() as db:
                    db.execute('DELETE FROM verification_requests WHERE request_id=? AND response IS NULL', (request_id,))
                raise
            with self.store.connection() as db:
                db.execute('UPDATE verification_requests SET response=? WHERE request_id=?', (json.dumps(response), request_id))
            return response

    def work_item(self, task_id):
        task = self.store.get(task_id)
        while task.get('parent_id'):
            task = self.store.get(task['parent_id'])
        root = task['id']
        tasks = self.store.list()
        included = {root}
        while True:
            children = {t['id'] for t in tasks if t.get('parent_id') in included}
            if children <= included:
                break
            included |= children
        attempts = []
        for t in tasks:
            if t['id'] not in included:
                continue
            depth, cursor = 1, t
            while cursor.get('parent_id'):
                depth += 1; cursor = self.store.get(cursor['parent_id'])
            with self.store.connection() as db:
                events = [dict(seq=r['seq'], kind=r['kind'], data=json.loads(r['data']), timestamp=r['timestamp'])
                          for r in db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 20', (t['id'],))][::-1]
            attempts.append(dict(task_id=t['id'], parent_id=t.get('parent_id'), attempt=depth, state=t['state'],
                                 session_id=t.get('session_id'), workspace=t.get('workspace'), criteria=(t.get('acceptance') or {}).get('criteria', []),
                                 **self.fields(t), checks=self.history(t['id']), artifacts=self.artifacts(t['id']), events=events))
        parents = {a['parent_id'] for a in attempts}
        leaves = [a for a in attempts if a['task_id'] not in parents]
        current = leaves[-1] if task.get('acceptance') else attempts[-1]
        status = current['work_status']
        actions = ['人工审核并调用 record_verification'] if status == 'needs_review' else ['调用 continue_task 续接返工'] if status == 'rework_required' else []
        return safe(dict(controller=self.controller(root), root_task_id=root, current_task_id=current['task_id'], work_status=status, attempts=attempts, pending_actions=actions))

    def expected_artifact(self, task, digest, path):
        return any(c['kind'] == 'sha256' and c.get('expected', '').lower() == digest
                   and c['path'] == path for c in task['acceptance']['criteria'])

    def root_id(self, task_id):
        task = self.store.get(task_id)
        while task.get('parent_id'):
            task = self.store.get(task['parent_id'])
        return task['id']

    def controller(self, root):
        with self.store.connection() as db:
            row = db.execute('SELECT controller_id,epoch FROM controllers WHERE root_task_id=?', (root,)).fetchone()
        return dict(row) if row else None

    def check_controller(self, task_id, controller_id=None, controller_epoch=None):
        lease = self.controller(self.root_id(task_id))
        if lease and (controller_id != lease['controller_id'] or type(controller_epoch) is not int
                      or controller_epoch != lease['epoch']):
            raise HubError('CONTROLLER_STALE', '总控租约已失效，请重新接管')

    def claim(self, request_id, task_id, controller_id):
        if not isinstance(controller_id, str) or not controller_id.strip() or len(controller_id) > 200:
            raise HubError('INVALID_ARGUMENT', 'controller_id 需要非空字符串，最多 200 字符')
        root = self.root_id(task_id)
        def apply():
            with self.store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT epoch FROM controllers WHERE root_task_id=?', (root,)).fetchone()
                epoch = row['epoch'] + 1 if row else 1
                db.execute('INSERT INTO controllers VALUES (?,?,?) ON CONFLICT(root_task_id) DO UPDATE SET controller_id=excluded.controller_id,epoch=excluded.epoch', (root, controller_id, epoch))
                response = dict(root_task_id=root, controller_id=controller_id, controller_epoch=epoch)
                db.execute('UPDATE verification_requests SET response=? WHERE request_id=?', (json.dumps(response), request_id))
            return response
        return self.request(request_id, dict(tool='claim_controller', task_id=task_id, controller_id=controller_id), apply)

    def external(self, request_id, task_id, verifier_id, checks):
        if (not isinstance(verifier_id, str) or not 0 < len(verifier_id) <= 200
                or not isinstance(checks, list) or not 1 <= len(checks) <= 100):
            raise HubError('INVALID_ARGUMENT', '外部验收参数不正确')
        def apply():
            task = self.store.get(task_id)
            criteria = (task.get('acceptance') or {}).get('criteria', [])
            ids = {c['id'] for c in criteria}
            artifacts = {a['artifact_id'] for a in self.artifacts(task_id)}
            seen = set()
            for c in checks:
                if (not isinstance(c, dict) or set(c) != {'criterion_id', 'outcome', 'reason', 'evidence'}
                        or not isinstance(c['criterion_id'], str) or c['criterion_id'] not in ids or c['criterion_id'] in seen
                        or not isinstance(c['outcome'], str) or c['outcome'] not in {'pass', 'fail', 'inconclusive'}
                        or not isinstance(c['reason'], str) or not 0 < len(c['reason']) <= 10000
                        or not isinstance(c['evidence'], list) or not c['evidence']
                        or any(not isinstance(a, str) or a not in artifacts for a in c['evidence'])):
                    raise HubError('INVALID_ARGUMENT', '检查必须引用已登记产物，条件、结论或原因不正确')
                seen.add(c['criterion_id'])
            superseded = self.work_item(task_id)['current_task_id'] != task_id
            run_id = uuid.uuid4().hex
            incoming = [dict(c, evidence={'artifacts': [a for a in self.artifacts(task_id) if a['artifact_id'] in c['evidence']]},
                             verifier={'name': verifier_id, 'version': 'external-1'}, checked_at=time.time(),
                             verification_run_id=run_id, superseded=superseded) for c in checks]
            if superseded:
                response = dict(task_id=task_id, superseded=True, **self.fields(task))
                with self.store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    for c in incoming:
                        db.execute('INSERT INTO verification_checks(task_id,data) VALUES (?,?)', (task_id, json.dumps(safe(self.store.safe(c)))))
                    db.execute('UPDATE verification_requests SET response=? WHERE request_id=?', (json.dumps(response), request_id))
                return response
            if task['state'] != 'succeeded' or not criteria:
                raise HubError('INVALID_STATE', '仅成功且请求验收的尝试可写入检查')
            old = {c['criterion_id']: c for c in self.summary(task).get('checks', [])}
            old.update({c['criterion_id']: c for c in incoming})
            for criterion in criteria:
                old.setdefault(criterion['id'], dict(criterion_id=criterion['id'], outcome='inconclusive', reason='缺少检查结果', evidence={}))
            merged = list(old.values())
            return self.save(task_id, self.decision(criteria, merged), merged, run_id, request_id,
                             history=incoming, extra=dict(task_id=task_id, superseded=False))
        def locked():
            with self.task_lock(task_id):
                return apply()
        return self.request(request_id, dict(tool='record_external_verification', task_id=task_id,
                            verifier_id=verifier_id, checks=checks), locked)

    def record(self, request_id, task_id, verifier_id, decision, reason, criterion_id=None, artifact_id=None):
        if (not isinstance(verifier_id, str) or not 0 < len(verifier_id) <= 200
                or not isinstance(decision, str) or decision not in {'pass', 'fail'}
                or not isinstance(reason, str) or not 0 < len(reason) <= 10000
                or (criterion_id is not None and (not isinstance(criterion_id, str) or not 0 < len(criterion_id) <= 200))
                or (artifact_id is not None and (not isinstance(artifact_id, str) or not 0 < len(artifact_id) <= 200))):
            raise HubError('INVALID_ARGUMENT', '人工裁决参数不正确')
        def apply():
            task = self.store.get(task_id)
            criteria = (task.get('acceptance') or {}).get('criteria', [])
            if criterion_id is not None and criterion_id not in {c['id'] for c in criteria}:
                raise HubError('INVALID_ARGUMENT', 'criterion_id 不存在')
            review_ids = {c['id'] for c in criteria if c['kind'] == 'content_review'}
            targets = {criterion_id} if criterion_id else {c['criterion_id'] for c in self.summary(task).get('checks', []) if c['outcome'] == 'inconclusive'}
            if targets & review_ids and artifact_id not in {a['artifact_id'] for a in self.artifacts(task_id)}:
                raise HubError('INVALID_ARGUMENT', 'content_review 必须引用已登记 evidence 的 artifact_id')
            superseded = self.work_item(task_id)['current_task_id'] != task_id
            summary = self.summary(task)
            if not superseded and summary['state'] != 'needs_review':
                raise HubError('INVALID_STATE', '只有 needs_review 可以人工裁决')
            run_id = uuid.uuid4().hex
            check = dict(criterion_id=criterion_id, outcome=decision, reason=reason, evidence={'artifact_id': artifact_id} if artifact_id else {},
                         verifier={'name': verifier_id, 'version': 'human-1'}, checked_at=time.time(),
                         verification_run_id=run_id, superseded=superseded)
            if superseded:
                response = dict(task_id=task_id, superseded=True, **self.fields(task))
                with self.store.connection() as db:
                    db.execute('INSERT INTO verification_checks(task_id,data) VALUES (?,?)', (task_id, json.dumps(safe(self.store.safe(check)))))
                    db.execute('UPDATE verification_requests SET response=? WHERE request_id=?', (json.dumps(response), request_id))
            else:
                checks = list(summary.get('checks', []))
                for old in checks:
                    if (criterion_id is None and old['outcome'] == 'inconclusive') or old['criterion_id'] == criterion_id:
                        old.update(outcome=decision, reason=reason, evidence=check['evidence'] or old.get('evidence', {}), verifier=check['verifier'], checked_at=check['checked_at'], verification_run_id=run_id)
                state = self.decision(criteria, checks)
                response = self.save(task_id, state, checks, run_id, request_id, history=[check],
                                     extra=dict(task_id=task_id, superseded=False))
            return response
        def apply_locked():
            with self.task_lock(task_id):
                return apply()
        return self.request(request_id, dict(tool='record_verification', task_id=task_id, verifier_id=verifier_id,
                            decision=decision, reason=reason, criterion_id=criterion_id, **({'artifact_id': artifact_id} if artifact_id is not None else {})), apply_locked)


def render(item):
    lines = [f"工作项 {item['root_task_id']}  当前 {item['current_task_id']}  {item['work_status']}"]
    for attempt in item['attempts']:
        lines.append(f"尝试 {attempt['attempt']} {attempt['task_id']} 执行={attempt['state']} 验收={attempt['verification']['state']}")
        for check in attempt['checks']:
            lines.append(f"  {check['criterion_id']}: {check['outcome']} {check['reason']}")
        for artifact in attempt['artifacts']:
            lines.append(f"  {artifact['logical_id']} sha256={artifact['sha256']} change={artifact.get('change', 'reused')}")
    return '\n'.join(lines)
