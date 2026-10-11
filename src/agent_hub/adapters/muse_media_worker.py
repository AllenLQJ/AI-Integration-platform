"""在 Muse 自有 Python 环境中通过认证 Noise 连接取回原图，不输出 URL。"""
import json
import os
import re
import sys
from pathlib import Path


def main():
    from muse_mcp.client import MuseClient
    from muse import ROUTES
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    root = Path(data['root']).resolve()
    client = MuseClient(reuse_gateway=True)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with client._gateway(client._config_loader()) as gateway:
            for item in data['images']:
                path = item['media_path']
                if (not re.fullmatch(r'/media/raw/workspace/imagine_media/[A-Za-z0-9_.-]+\.(?:png|jpe?g)', path)
                        or item['vm_id'] != gateway.vm_id
                        or type(item['size_bytes']) is not int or not 0 < item['size_bytes'] <= 64 * 1024 * 1024):
                    raise ValueError('invalid media binding')
                output = root / item['filename']
                if output.parent != root or output.is_symlink():
                    raise ValueError('invalid output path')
                # 路径来自实测 image.presentation 的 original 字段；不接受客户端任意 URL。
                ROUTES['hub.media.original'] = {'http': 'GET', 'path': path}
                content = gateway.request('hub.media.original', timeout=data['timeout_seconds'])
                if len(content) != item['size_bytes']:
                    raise ValueError('media size mismatch')
                temporary = output.with_suffix('.part')
                try:
                    with temporary.open('xb') as file:
                        os.chmod(temporary, 0o600)
                        file.write(content)
                    temporary.replace(output)
                finally:
                    if temporary.exists(): temporary.unlink()
        print(json.dumps({'hub_result': {'ok': True}}), flush=True)
    except Exception:
        print(json.dumps({'hub_result': {'ok': False, 'error_code': 'ARTIFACT_FETCH_FAILED',
                                        'error': 'Muse 授权原图取回失败；未重发生成'}}), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
