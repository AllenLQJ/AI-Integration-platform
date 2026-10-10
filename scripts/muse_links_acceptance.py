"""Continue the previously verified Muse session once, preserving request id."""
import json
import time
from agent_hub.client import Client
from agent_hub.config import data_dir

root = data_dir()
c = Client(root)
# Reload the daemon explicitly before this probe when its source has changed.
# A probe must never stop a shared daemon: another frontend may submit concurrently.
previous = json.loads((root / 'lifecycle-acceptance.json').read_text(encoding='utf-8'))
parent = previous['muse_follow']['task_id']
task = c.call('continue_task', dict(request_id='muse-links-20261010-2', task_id=parent,
    prompt='请用一句话说明 SQLite 的事务作用，并附 SQLite 事务的官方资料链接。', timeout_seconds=180))
print('task_id=' + task['id'], flush=True)
result = {'ready': False}
for _ in range(110):
    result = c.call('get_result', {'task_id': task['id']})
    if result['ready']:
        break
    time.sleep(2)
summary = dict(task_id=task['id'], result=result)
(root / 'muse-links-acceptance-20261010-v2.json').write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False), flush=True)
if not (result.get('state') == 'succeeded' and
        any(url.startswith('https://www.sqlite.org/') for url in result.get('source_urls', [])) and
        result.get('completion_evidence', {}).get('stable_polls') == 2):
    raise SystemExit('Muse link acceptance did not pass')
