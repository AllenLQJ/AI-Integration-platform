"""Persistent node declarations and controlled, local receiver transport.

Receiver bindings are private in-process injection points, never RPC arguments.
SSH configuration is a declaration, not proof of connectivity.
"""
import hashlib
import json
import platform
import os
import threading
import time
import uuid
from pathlib import Path
from .models import HubError


class Nodes:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.receivers = {}
        self.lock = threading.RLock()
        self.timeout_seconds = 30
        from .config import artifact_transfer_options
        for key, value in artifact_transfer_options(service.root).items():
            setattr(self, key, value)
        self.local_id = 'local-' + service.gateway.host_id
        with self.store.connection(write=True) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS nodes (node_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                    host_id TEXT NOT NULL, capabilities TEXT NOT NULL, transport TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_transfers (request_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL, target_node_id TEXT NOT NULL, state TEXT NOT NULL, response TEXT);
                CREATE TABLE IF NOT EXISTS artifact_receipts (request_id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(artifact_receipts)')}
            if 'sender_host_id' not in columns:
                db.execute('ALTER TABLE artifact_receipts RENAME TO artifact_receipts_legacy')
                db.execute('CREATE TABLE artifact_receipts (sender_host_id TEXT NOT NULL, request_id TEXT NOT NULL, '
                           'data TEXT NOT NULL, PRIMARY KEY(sender_host_id, request_id))')
                db.execute("INSERT INTO artifact_receipts SELECT 'legacy-unattributed', request_id, data FROM artifact_receipts_legacy")
                db.execute('DROP TABLE artifact_receipts_legacy')
            db.execute('CREATE TABLE IF NOT EXISTS received_blobs (sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, size_bytes INTEGER NOT NULL)')
            if 'sender_host_id' not in columns:
                # Preserve verified v1 files in the content index rather than duplicating them on a new key.
                root = service.root.resolve() / 'received-artifacts'
                if not root.is_symlink():
                    for row in db.execute("SELECT request_id, data FROM artifact_receipts WHERE sender_host_id='legacy-unattributed' LIMIT ?", (self.max_received_files,)):
                        metadata = json.loads(row['data'])
                        name = hashlib.sha256(row['request_id'].encode()).hexdigest()
                        try:
                            content = self.read_bytes(root / name)
                        except (OSError, HubError):
                            continue
                        digest = hashlib.sha256(content).hexdigest()
                        if digest == metadata.get('sha256') and len(content) == metadata.get('size_bytes'):
                            db.execute('INSERT OR IGNORE INTO received_blobs VALUES (?,?,?)', (digest, name, len(content)))
            db.execute("UPDATE artifact_transfers SET state='retryable' WHERE state='transferring'")
            # Declarations are refreshed at startup; obsolete configuration cannot route tasks.
            db.execute('DELETE FROM nodes')
        local = {}
        from .adapters.remote import Remote
        config = service.gateway.config.get('agents', {})
        for name, adapter in service.adapters.items():
            modes = [m for m in adapter.capabilities if m != 'continue']
            if isinstance(adapter, Remote):
                item = config.get(name, {})
                from .config import remote_node_declaration
                self.register(*remote_node_declaration(name, item, adapter.client.host), {name: modes}, 'ssh')
            else:
                local[name] = modes
        self.register(self.local_id, {'Windows': 'windows', 'Darwin': 'darwin', 'Linux': 'linux'}.get(platform.system()),
                      service.gateway.host_id, local, 'local')
        self.receivers[self.local_id] = self

    def register(self, node_id, os_platform, host_id, capabilities, transport):
        if (not isinstance(node_id, str) or not 0 < len(node_id) <= 200
                or not isinstance(os_platform, str) or os_platform not in {'darwin', 'windows', 'linux'}
                or not isinstance(transport, str) or transport not in {'local', 'ssh'}
                or not isinstance(host_id, str) or not host_id or not isinstance(capabilities, dict)
                or any(not isinstance(k, str) or not isinstance(v, list) or
                       any(m not in {'text_only', 'read_only', 'edit_worktree', 'delegate'} for m in v)
                       for k, v in capabilities.items())):
            raise HubError('INVALID_CONFIG', '节点声明不正确')
        if transport != 'local' and host_id == self.service.gateway.host_id:
            raise HubError('INVALID_CONFIG', '非 local 节点不能声明本机 host_id')
        with self.store.connection(write=True) as db:
            old = db.execute('SELECT * FROM nodes WHERE node_id=?', (node_id,)).fetchone()
            if old:
                if (old['platform'], old['host_id'], old['transport']) != (os_platform, host_id, transport):
                    raise HubError('INVALID_CONFIG', '同一节点声明冲突')
                capabilities = dict(json.loads(old['capabilities']), **capabilities)
            db.execute('INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?)',
                       (node_id, os_platform, host_id, json.dumps(capabilities), transport))

    def list(self):
        with self.store.connection() as db:
            return {'nodes': [dict(r, capabilities=json.loads(r['capabilities']),
                                   remote_connection_verified=False) for r in db.execute('SELECT * FROM nodes ORDER BY node_id')]}

    def get(self, node_id):
        for node in self.list()['nodes']:
            if node['node_id'] == node_id:
                return node
        raise HubError('NODE_UNAVAILABLE', '节点不存在或未声明所需能力')

    def source(self, artifact_id):
        ident = artifact_id.removeprefix('artifact://')
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM artifact_index WHERE id=?', (ident,)).fetchone()
            media = db.execute('SELECT * FROM media_artifacts WHERE id=?', (ident,)).fetchone()
        if row:
            data = json.loads(row['data'])
            if data['source'] == 'media':
                return self.source(data['media_artifact_id'])
            task = self.store.get(row['task_id'])
            root = Path(task.get('workspace') or task['cwd']).resolve()
            path = root / data['path']
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise HubError('ARTIFACT_NOT_FOUND', '产物路径越界')
            for parent in path.parents:
                if parent == root:
                    break
                if parent.is_symlink():
                    raise HubError('ARTIFACT_NOT_FOUND', '产物路径不允许符号链接')
            return row['task_id'], data, path
        if media:
            data = json.loads(media['data'])
            return media['task_id'], data, self.service.gateway.checked_file(data['path'])
        raise HubError('ARTIFACT_NOT_FOUND', '产物未登记')

    def receive(self, request_id, artifact_id, node_id, content, metadata, *, sender_host_id,
                deadline=None, superseded=False):
        """Sender-scoped receipts, receiver-checked content and bounded blob storage."""
        if not isinstance(sender_host_id, str) or not 0 < len(sender_host_id) <= 200:
            raise HubError('INVALID_ARGUMENT', '缺少有效发送方 host_id')
        digest = hashlib.sha256(content).hexdigest()
        if digest != metadata['sha256'] or len(content) != metadata['size_bytes']:
            raise HubError('ARTIFACT_HASH_MISMATCH', '接收端产物哈希或大小不一致')
        def checkpoint():
            if self.service.stopping.is_set() or deadline is not None and time.monotonic() >= deadline:
                raise HubError('TRANSFER_TIMED_OUT', '接收确认超时或服务停止')
        with self.lock:
            checkpoint()
            with self.store.connection() as db:
                old = db.execute('SELECT data FROM artifact_receipts WHERE sender_host_id=? AND request_id=?',
                                 (sender_host_id, request_id)).fetchone()
                blob = db.execute('SELECT * FROM received_blobs WHERE sha256=?', (digest,)).fetchone()
                receipt_count = db.execute('SELECT count(*) FROM artifact_receipts').fetchone()[0]
            if old:
                receipt = json.loads(old['data'])
                if (receipt['artifact_id'], receipt['target_node_id'], receipt['sha256']) != (artifact_id, node_id, digest):
                    raise HubError('IDEMPOTENCY_CONFLICT', '接收请求键已用于不同产物')
                return receipt
            if receipt_count >= self.max_received_files:
                raise HubError('ARTIFACT_QUOTA_EXCEEDED', '接收收据总数已达到上限')
            root = self.service.root.resolve() / 'received-artifacts'
            root.mkdir(exist_ok=True)
            if root.is_symlink():
                raise HubError('ARTIFACT_NOT_FOUND', '接收目录不允许符号链接')
            prefix = hashlib.sha256(sender_host_id.encode()).hexdigest()
            # A blob keeps its first sender's namespace; the digest index shares it across senders.
            name = blob['filename'] if blob else prefix + '-' + digest
            if blob or (root / name).exists():
                actual = self.read_bytes(root / name)
                if hashlib.sha256(actual).hexdigest() != digest or len(actual) != len(content):
                    raise HubError('ARTIFACT_HASH_MISMATCH', '已接收文件哈希不一致')
                if not blob:
                    with self.store.connection(write=True) as db:
                        db.execute('INSERT INTO received_blobs VALUES (?,?,?)', (digest, name, len(content)))
            else:
                size, count = 0, 0
                with os.scandir(root) as entries:
                    for entry in entries:
                        checkpoint()
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            raise HubError('ARTIFACT_NOT_FOUND', '接收目录含未授权路径')
                        count += 1
                        size += entry.stat(follow_symlinks=False).st_size
                        if count >= self.max_received_files or size + len(content) > self.max_received_bytes:
                            raise HubError('ARTIFACT_QUOTA_EXCEEDED', '接收产物总量已达到上限')
                if size + len(content) > self.max_received_bytes:
                    raise HubError('ARTIFACT_QUOTA_EXCEEDED', '接收产物总量已达到上限')
                request_key = hashlib.sha256((sender_host_id + ':' + request_id).encode()).hexdigest()
                temporary = root / (request_key + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    with temporary.open('xb') as file:
                        file.write(content)
                    actual = self.read_bytes(temporary)
                    if hashlib.sha256(actual).hexdigest() != digest or len(actual) != len(content):
                        raise HubError('ARTIFACT_HASH_MISMATCH', '接收文件哈希不一致')
                    checkpoint()
                    temporary.replace(root / name)
                finally:
                    temporary.unlink(missing_ok=True)
                with self.store.connection(write=True) as db:
                    db.execute('INSERT INTO received_blobs VALUES (?,?,?)', (digest, name, len(content)))
            checkpoint()
            receipt = dict(artifact_id=artifact_id, target_node_id=node_id, sha256=digest,
                           sender_host_id=sender_host_id, size_bytes=len(content),
                           received_at=time.time(), superseded=superseded)
            with self.store.connection(write=True) as db:
                db.execute('INSERT INTO artifact_receipts VALUES (?,?,?)', (sender_host_id, request_id, json.dumps(receipt)))
                checkpoint()
            return receipt

    def transfer(self, request_id, artifact_id, target_node_id):
        if any(not isinstance(v, str) or not 0 < len(v) <= 200 for v in (request_id, artifact_id, target_node_id)):
            raise HubError('INVALID_ARGUMENT', '传输参数需要非空字符串，最多 200 字符')
        with self.lock:
            with self.store.connection() as db:
                old = db.execute('SELECT * FROM artifact_transfers WHERE request_id=?', (request_id,)).fetchone()
                if old:
                    if (old['artifact_id'], old['target_node_id']) != (artifact_id, target_node_id):
                        raise HubError('IDEMPOTENCY_CONFLICT', '传输请求键已用于不同内容')
                    if old['response']:
                        return json.loads(old['response'])
            node = self.get(target_node_id)
            receiver = self.receivers.get(target_node_id)
            if node['transport'] == 'ssh':
                raise HubError('NODE_UNVERIFIED', 'SSH 节点连接与产物传输尚未验证')
            if receiver is None:
                raise HubError('NODE_UNAVAILABLE', '没有受控接收端')
            with self.store.connection(write=True) as db:
                db.execute("INSERT INTO artifact_transfers VALUES (?,?,?,'transferring',NULL) ON CONFLICT(request_id) DO UPDATE SET state='transferring'",
                           (request_id, artifact_id, target_node_id))
            end = time.monotonic() + self.timeout_seconds
            try:
                task_id, metadata, path = self.source(artifact_id)
                if path.stat().st_size > 64 * 1024 * 1024:
                    raise HubError('INVALID_ARTIFACT', '产物超过 64 MiB 传输上限')
                content = self.read_bytes(path)
                # No receiver is invoked after timeout or shutdown.
                if time.monotonic() >= end or self.service.stopping.is_set():
                    raise HubError('TRANSFER_TIMED_OUT', '传输未确认，可按原请求键重试')
                root_id = self.service.verification.root_id(task_id)
                with self.service.verification.task_lock(root_id):
                    superseded = self.service.verification.work_item(task_id)['current_task_id'] != task_id
                    receipt = receiver.receive(request_id, artifact_id, target_node_id, content, metadata, sender_host_id=self.service.gateway.host_id, deadline=end, superseded=superseded)
                    if time.monotonic() >= end or self.service.stopping.is_set():
                        # A receiver may have durable bytes, but sender must not report delivery.
                        raise HubError('TRANSFER_TIMED_OUT', '接收确认超时，可按原请求键核对重试')
                    receipt = dict(receipt, superseded=superseded)
                    response = dict(state='superseded' if superseded else 'delivered', receipt=receipt)
                    with self.store.connection(write=True) as db:
                        db.execute('UPDATE artifact_transfers SET state=?, response=? WHERE request_id=?',
                                   (response['state'], json.dumps(response), request_id))
                    return response
            except FileNotFoundError:
                with self.store.connection(write=True) as db:
                    db.execute("UPDATE artifact_transfers SET state='retryable' WHERE request_id=?", (request_id,))
                raise HubError('ARTIFACT_NOT_FOUND', '登记产物文件缺失') from None
            except Exception:
                with self.store.connection(write=True) as db:
                    db.execute("UPDATE artifact_transfers SET state='retryable' WHERE request_id=? AND response IS NULL", (request_id,))
                raise


    @staticmethod
    def read_bytes(path):
        """Open each POSIX path component without following links; bound memory."""
        path = path.absolute()
        if os.name != 'nt':
            fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in path.parts[1:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(child, 'rb') as file:
                    content = file.read(64 * 1024 * 1024 + 1)
            finally:
                os.close(fd)
        else:
            before = path.stat()
            with path.open('rb') as file:
                opened = os.fstat(file.fileno())
                content = file.read(64 * 1024 * 1024 + 1)
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise HubError('ARTIFACT_EXPIRED', '读取期间产物改变')
        if len(content) > 64 * 1024 * 1024:
            raise HubError('INVALID_ARTIFACT', '产物超过传输上限')
        return content

    def artifact(self, artifact_id, target_host_id=None, target_transport=None):
        ident = artifact_id.removeprefix('artifact://')
        with self.store.connection() as db:
            media = db.execute('SELECT id FROM media_artifacts WHERE id=?', (ident,)).fetchone()
        if target_transport is None:
            target_transport = 'local' if target_host_id is None or target_host_id == self.service.gateway.host_id else 'ssh'
            if target_host_id is not None:
                matches = [n for n in self.list()['nodes'] if n['host_id'] == target_host_id]
                if any(n['transport'] != 'local' for n in matches):
                    target_transport = 'ssh'
        if media:
            return self.service.gateway.artifact(ident, target_host_id, target_transport)
        _, metadata, path = self.source(artifact_id)
        content = self.read_bytes(path)
        if hashlib.sha256(content).hexdigest() != metadata['sha256'] or len(content) != metadata['size_bytes']:
            raise HubError('ARTIFACT_EXPIRED', '登记产物内容已变化')
        transfer = dict(kind='transfer_required', host_id=self.service.gateway.host_id)
        if target_transport == 'local' and (target_host_id is None or target_host_id == self.service.gateway.host_id):
            transfer.update(kind='local_file', path=str(path), allowed_root=str(path.parent))
        public = dict(metadata)
        public.pop('path', None)
        return dict(contract_version='1.0', artifact=public, transfer=transfer)
