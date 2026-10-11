"""由 Muse 结构化 image 展示取回真实附件；从不下载正文来源链接。"""
import re
from urllib.parse import urlsplit
from ..models import HubError

MAX_BYTES = 64 * 1024 * 1024


def images_for_task(history, baseline):
    images = []
    for row in sorted(history, key=lambda x: x.get('seq') or 0):
        if (row.get('seq') or 0) <= baseline:
            continue
        if row.get('role') in {'user', 'message.user'}:
            images = []  # 只接受最后一轮用户请求后的附件。
            continue
        payload = row.get('raw', {}).get('payload', {})
        if row.get('event_name') != 'delta.presentation' or payload.get('kind') != 'image':
            continue
        for image in payload.get('data', {}).get('images', []):
            if not isinstance(image, dict):
                raise HubError('INVALID_ARTIFACT', 'Muse 图片附件格式不正确')
            if image.get('missing') is True:
                raise HubError('ARTIFACT_NOT_FOUND', 'Muse 成品文件缺失')
            mime = image.get('mime')
            size = image.get('byte_len')
            url = image.get('variants', {}).get('original')
            if mime not in {'image/png', 'image/jpeg'} or type(size) is not int or not 0 < size <= MAX_BYTES:
                raise HubError('INVALID_ARTIFACT', 'Muse 图片格式或大小不支持')
            parsed = urlsplit(url) if isinstance(url, str) else None
            # 实测授权原图来自专用 VM 域名；拒绝来源网页、任意地址和重定向。
            if (not parsed or parsed.scheme != 'https' or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or not re.fullmatch(
                        r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.metaaivm\.com', parsed.hostname or '')):
                raise HubError('INVALID_ARTIFACT', 'Muse 原图取回地址不在已验证来源范围')
            remote = urlsplit(image.get('path', ''))
            if (remote.scheme != 'sandbox' or remote.netloc != 'workspace'
                    or parsed.path != '/media/raw/workspace' + remote.path
                    or not re.fullmatch(r'/imagine_media/[A-Za-z0-9_.-]+\.(?:png|jpe?g)', remote.path)):
                raise HubError('INVALID_ARTIFACT', 'Muse 图片缺少可核对的授权原图路径')
            item = {'url': url, 'media_path': parsed.path, 'vm_id': parsed.hostname.split('.')[0],
                    'mime_type': mime, 'size_bytes': size,
                    'presentation_id': payload.get('id'), 'seq': row['seq']}
            if item not in images:
                images.append(item)
    return images
