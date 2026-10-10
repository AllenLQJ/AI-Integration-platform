"""Read-only gateway probe; never print tokens, cookies or exception messages."""
import json
import os
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from agent_hub.config import data_dir

config = json.loads((data_dir() / 'config.json').read_text(encoding='utf-8'))['agents']['muse']
os.environ.update(config.get('env', {}))
if '--socks' in sys.argv:
    proxy = urlsplit(config.get('env', {}).get('HTTPS_PROXY', ''))
    if proxy.hostname not in {'127.0.0.1', 'localhost'} or not proxy.port or proxy.username or proxy.password:
        raise ValueError('SOCKS probe requires a local credential-free mixed proxy port')
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        os.environ[key] = 'socks5h://' + proxy.hostname + ':' + str(proxy.port)
if '--direct' in sys.argv:
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
sys.path.insert(0, str(Path(config['executable']).parents[2]))
from muse_mcp.client import MuseClient

client = MuseClient()
stage = 'connect'
try:
    with client._gateway(client._config_loader()) as gateway:
        print(json.dumps({'stage': stage, 'ok': True}), flush=True)
        stage = 'history'
        result = gateway.call_json('chat.history', body={'limit': 1})
        print(json.dumps({'stage': stage, 'ok': True,
                          'event_count': len(result.get('chat_events', []))}), flush=True)
except Exception as error:
    details = {'stage': stage, 'ok': False, 'exception_type': type(error).__name__,
               'frames': [frame.name for frame in traceback.extract_tb(error.__traceback__)]}
    for key in ('code', 'status'):
        value = getattr(error, key, None)
        if isinstance(value, int): details[key] = value
    print(json.dumps(details), flush=True)
    raise SystemExit(1)
