"""The same example through the language-neutral command bridge protocol."""
import json
from pathlib import Path
import sys
import time
import uuid


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python -m agent_hub_echo REQUEST_FILE')
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if request.get('schema') != 'agent-hub.command.v1' or not isinstance(request.get('prompt'), str):
        raise SystemExit('Invalid request')
    if time.time() >= request['deadline']:
        raise SystemExit('Request expired')
    print(json.dumps({'answer': request['prompt'],
                      'session_id': request.get('resume_session') or uuid.uuid4().hex}, ensure_ascii=False))


if __name__ == '__main__':
    main()
