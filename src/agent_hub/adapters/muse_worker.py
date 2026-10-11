"""Private Muse worker: parent owns cancellation of this entire process tree."""
import asyncio
import json
import sys
import threading
from pathlib import Path
from .muse import Muse
from ..models import HubError


def main():
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    adapter = Muse(data['executable'], data['args'], data['env'], data.get('session_id'))
    def event(kind, payload):
        print(json.dumps({'hub_event': {'kind': kind, 'data': payload}}, ensure_ascii=False), flush=True)
    try:
        result = asyncio.run(adapter.execute(data['task'], threading.Event(), event))
    except Exception as error:
        leaf = adapter.unwrap(error)
        result = dict(leaf.details) if isinstance(leaf, HubError) else {}
        result.update({'error_code': leaf.code if isinstance(leaf, HubError) else 'MUSE_ERROR',
                  'error': str(leaf) if isinstance(leaf, HubError) else type(leaf).__name__})
    print(json.dumps({'hub_result': result}, ensure_ascii=False), flush=True)

if __name__ == '__main__': main()
