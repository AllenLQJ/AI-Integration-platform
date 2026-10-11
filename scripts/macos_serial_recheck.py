"""同工作区短任务真实串行完成复验，保留长任务超时记录。"""
import json,time
from agent_hub.client import Client
from agent_hub.config import private_dir,data_dir
import macos_acceptance as m
m.ROOT=data_dir()/'mac-serial-recheck-20261011';private_dir(m.ROOT)
(m.ROOT/'config.json').write_text((data_dir()/'config.json').read_text());(m.ROOT/'config.json').chmod(0o600)
m.c=Client(m.ROOT);p=m.repo('同目录')
a=m.submit('serial-a','codex','Reply exactly SERIAL_A. Do not use tools.',p,120)
b=m.submit('serial-b','claude','Reply exactly SERIAL_B. Do not use tools.',p,120,'text_only')
snapshots=[];overlap=False;end=time.monotonic()+260
while time.monotonic()<end:
 states=[m.c.call('get_task',{'task_id':t['id']})['state'] for t in (a,b)]
 if not snapshots or snapshots[-1]['states']!=states:snapshots.append(dict(time=time.time(),states=states))
 if states==['running','running']:overlap=True
 if all(s in {'succeeded','failed','timed_out'} for s in states):break
 time.sleep(.1)
r=[m.wait(t) for t in (a,b)]
m.save('serial',dict(task_ids=[a['id'],b['id']],snapshots=snapshots,results=r,overlap=overlap,passed=not overlap and all(x['state']=='succeeded' for x in r)))
m.c.stop()
