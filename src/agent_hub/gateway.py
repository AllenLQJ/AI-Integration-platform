"""HubGateway v1：保守能力声明与服务端授权产物登记。"""
import hashlib
import json
import mimetypes
import platform
import uuid
from pathlib import Path
from . import __version__
from PIL import Image
import warnings
from .models import HubError

CONTRACT_VERSION = '1.0'
KINDS = {'text.review', 'image.generate', 'image.edit', 'video.generate',
         'image.review', 'video.frames.review', 'video.full.review'}


class Gateway:
    def __init__(self, root, store):
        self.store = store
        self.root = Path(root).resolve()
        self.artifact_root = self.root / 'media-artifacts'
        config = self.root / 'config.json'
        self.config = json.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
        # 同主机各数据目录使用同一标识；不将机器名称直接暴露给客户端。
        self.host_id = 'host-' + hashlib.sha256((platform.node() + ':' + str(uuid.getnode())).encode()).hexdigest()[:32]
        with store.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS media_artifacts '
                       '(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, data TEXT NOT NULL)')

    def capabilities(self, adapters):
        agents = {}
        for name, adapter in adapters.items():
            declared = self.config.get('agents', {}).get(name, {}).get('media_capabilities', [])
            capabilities = []
            for item in declared:
                if (not isinstance(item, dict) or item.get('kind') not in KINDS
                        or not isinstance(item.get('model'), str) or not item['model']
                        or item.get('verification') not in {'verified', 'unverified', 'unsupported'}
                        or not isinstance(item.get('parameters', {}), dict)
                        or not isinstance(item.get('output_mime_types', []), list)):
                    raise HubError('INVALID_CONFIG', '媒体能力声明不正确')
                evidence = item.get('evidence')
                if item['verification'] == 'verified' and (not isinstance(evidence, dict)
                        or not all(evidence.get(k) for k in ('task_id', 'date', 'scope'))):
                    raise HubError('INVALID_CONFIG', '已验证能力需要任务、日期和覆盖范围证据')
                # 首版尚无生产连接器输入附件适配器，不能靠配置开启。
                if item.get('input_transports', []):
                    raise HubError('INVALID_CONFIG', '首版尚未实现输入附件传输适配器')
                capabilities.append(dict(item, evidence=evidence, parameters=item.get('parameters', {}),
                                         input_transports=[], output_mime_types=item.get('output_mime_types', [])))
            agents[name] = {'available': adapter.diagnose().get('available', False),
                            'modes': [m for m in adapter.capabilities if m != 'continue'],
                            'capabilities': capabilities}
        return {'contract_version': CONTRACT_VERSION, 'service_version': __version__,
                'host_id': self.host_id, 'agents': agents}

    @staticmethod
    def context(value):
        if value is None:
            return
        if not isinstance(value, dict) or value.get('contract_version') != CONTRACT_VERSION:
            raise HubError('INVALID_ARGUMENT', 'context 需要 contract_version=1.0')
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError):
            raise HubError('INVALID_ARGUMENT', 'context 必须为有效 JSON 对象') from None
        payload = value.get('payload', {})
        if not isinstance(payload, dict) or not isinstance(payload.get('attachments', []), list):
            raise HubError('INVALID_ARGUMENT', 'context.payload.attachments 需要数组')
        if payload.get('input_host_id') is not None and (
                not isinstance(payload['input_host_id'], str) or not payload['input_host_id']):
            raise HubError('INVALID_ARGUMENT', 'input_host_id 需要非空字符串或 null')
        if payload.get('attachments'):
            raise HubError('UNSUPPORTED_INPUT_TRANSPORT', '尚无已验证的输入附件传输适配器')

    def checked_file(self, path):
        path = Path(path)
        root = self.artifact_root.resolve()
        try:
            relative = path.absolute().relative_to(self.artifact_root.absolute())
            cursor = self.artifact_root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError()
            if self.artifact_root.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError()
            return path.resolve()
        except (ValueError, OSError):
            raise HubError('ARTIFACT_NOT_FOUND', '附件不存在或不在授权目录') from None

    def register(self, task_id, entries):
        """仅接受适配器明确返回的媒体附件；来源网址不参与登记。"""
        if not isinstance(entries, list):
            raise HubError('INVALID_ARTIFACT', '媒体附件需要数组')
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                raise HubError('INVALID_ARTIFACT', '适配器附件缺少授权文件')
            path = self.checked_file(entry['path'])
            if path.stat().st_size > 64 * 1024 * 1024:
                raise HubError('INVALID_ARTIFACT', '附件超过首版 64 MiB 上限')
            content = path.read_bytes()
            mime = entry.get('mime_type') or mimetypes.guess_type(path.name)[0]
            if not isinstance(mime, str) or not mime.startswith(('image/', 'video/')):
                raise HubError('INVALID_ARTIFACT', '附件缺少媒体格式')
            # 验证首版支持的实际文件签名，不能仅凭扩展名标完成。
            signatures = {'image/png': content.startswith(b'\x89PNG\r\n\x1a\n'),
                          'image/jpeg': content.startswith(b'\xff\xd8\xff'),
                          'video/mp4': False}
            if not signatures.get(mime, False):
                raise HubError('INVALID_ARTIFACT', '附件格式不支持或签名不正确')
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(path) as image:
                        if image.format != ('PNG' if mime == 'image/png' else 'JPEG'):
                            raise ValueError()
                        image.verify()
                    with Image.open(path) as image:
                        image.load()
            except Exception:
                raise HubError('INVALID_ARTIFACT', '附件无法解码') from None
            digest = hashlib.sha256(content).hexdigest()
            ident = 'artifact-' + uuid.uuid5(uuid.NAMESPACE_URL, task_id + ':' + str(path)).hex
            metadata = dict(artifact_id=ident, task_id=task_id, filename=path.name,
                            mime_type=mime, size_bytes=len(content), sha256=digest)
            with self.store.connection() as db:
                existing = db.execute('SELECT data FROM media_artifacts WHERE id=?', (ident,)).fetchone()
                if existing and json.loads(existing['data']) != dict(metadata, path=str(path)):
                    raise HubError('ARTIFACT_EXPIRED', '已登记附件内容已变化')
                db.execute('INSERT OR IGNORE INTO media_artifacts VALUES (?,?,?)',
                           (ident, task_id, json.dumps(dict(metadata, path=str(path)))))
            result.append(metadata)
        return result

    def artifact(self, artifact_id):
        with self.store.connection() as db:
            row = db.execute('SELECT data FROM media_artifacts WHERE id=?', (artifact_id,)).fetchone()
        if not row:
            raise HubError('ARTIFACT_NOT_FOUND', '附件不存在')
        data = json.loads(row['data'])
        path = self.checked_file(data.pop('path'))
        content = path.read_bytes()
        if len(content) != data['size_bytes'] or hashlib.sha256(content).hexdigest() != data['sha256']:
            raise HubError('ARTIFACT_EXPIRED', '附件内容已变化')
        return {'contract_version': CONTRACT_VERSION, 'artifact': data,
                'transfer': {'kind': 'local_file', 'host_id': self.host_id,
                             'path': str(path), 'allowed_root': str(self.artifact_root.resolve())}}


def result_fields(state, result):
    if state not in {'queued', 'running', 'waiting_for_user', 'cancelling', 'succeeded', 'failed', 'timed_out',
                     'interrupted', 'delivery_unknown', 'cancelled'}:
        raise HubError('PROTOCOL_ERROR', '未知任务状态')
    uncertain = state in {'interrupted', 'delivery_unknown'} or (
        state in {'cancelled', 'timed_out'} and not result.get('remote_cancelled')
        and result.get('cancel_scope') != 'not_started')
    code = 'TASK_TIMED_OUT' if state == 'timed_out' else result.get('error_code')
    mapped = 'running' if state in {'cancelling', 'waiting_for_user'} else 'failed' if state in {
        'timed_out', 'interrupted', 'delivery_unknown', 'cancelled'} else state
    return {'contract_version': CONTRACT_VERSION, 'gateway_state': mapped, 'media_artifacts': result.get('media_artifacts', []),
            'execution_uncertain': uncertain,
            'errors': [{'code': code or 'TASK_FAILED', 'message': result.get('error', '任务失败'),
                        'retryable': False, 'details': {}}] if code or result.get('error') else []}
