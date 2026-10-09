"""Scoped real CLI cancellation/timeout plus Muse native continuation."""
import json
import time
from pathlib import Path
from agent_hub.client import Client
from agent_hub.config import data_dir


root = data_dir()
c = Client(root)
repo = next((root / 'acceptance').iterdir())
results = {}
for name, limit in [('cancel', 120), ('timeout', 10)]:
    task = c.call('submit_task', dict(request_id='lifecycle-'+name+'-20261009-1', agent='codex',
        cwd=str(repo), mode='read_only', timeout_seconds=limit,
        prompt='Without using tools, write a detailed explanation of database transaction isolation.'))
    started = time.time()
    if name == 'cancel':
        for _ in range(100):
            ev = c.call('get_events', {'task_id': task['id']})['events']
            if any(e['kind'] == 'process' for e in ev): break
            time.sleep(.1)
        results['cancel_request'] = c.call('cancel_task', {'task_id':task['id']})
    while time.time()-started < 40:
        result = c.call('get_result', {'task_id':task['id']})
        if result['ready']: break
        time.sleep(.2)
    results[name] = {'task_id':task['id'], 'result':result, 'elapsed':round(time.time()-started,2)}
    print(name,result.get('state'),flush=True)
previous = json.loads((root/'acceptance-v2-muse.json').read_text(encoding='utf-8'))
task = c.call('continue_task', dict(request_id='muse-follow-worker-20261009-1', task_id=previous['task_id'],
    prompt='上一轮我要求你记住的编号是什么？回答编号，并给出 SQLite 事务的官方文档链接。', timeout_seconds=120))
for _ in range(70):
    result = c.call('get_result', {'task_id':task['id']})
    if result['ready']: break
    time.sleep(2)
results['muse_follow'] = {'task_id':task['id'], 'result':result}
(root/'lifecycle-acceptance.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False),flush=True)
