"""原 Muse 等待进程停止后，仅核对和取消其原会话。"""
import asyncio
import json
import sys
from pathlib import Path
from .muse import Muse


def main():
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    adapter = Muse(data['executable'], data['args'], data['env'])
    try:
        result = asyncio.run(adapter.cancel_remote(data['task'], data['cancel_context']))
    except Exception:
        result = {'remote_cancelled': False, 'warning': 'Muse 取消连接未确认；保留会话锁'}
    print(json.dumps({'hub_result': result}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
