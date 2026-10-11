"""真实后台崩溃后，同目录排队隔离与其他目录排队恢复。"""
import json,os,signal,time
import macos_acceptance as m
from agent_hub.client import Client
from agent_hub.config import private_dir,data_dir
m.ROOT=data_dir()/'mac-crash-queue-20261011';private_dir(m.ROOT)
config=json.loads((data_dir()/'config.json').read_text());config['concurrency']={'global':1,'per_agent':{'codex':1,'claude':1}}
(m.ROOT/'config.json').write_text(json.dumps(config));(m.ROOT/'config.json').chmod(0o600)
m.c=Client(m.ROOT);p=m.repo('旧工作区');safe=m.repo('安全工作区')
t=m.submit('running','codex','Run /bin/sleep 90 now; authorized crash test, no file changes.',p,180)
observed=[];end=time.monotonic()+100
while time.monotonic()<end:
 ev=m.c.call('get_events',{'task_id':t['id'],'limit':200})['events'];roots=[e['data']['pid'] for e in ev if e['kind']=='process']
 if roots:
  tree=m.descendants(roots[0],m.processes())
  if any(r['command']=='/bin/sleep 90' for r in tree):observed=tree;break
 time.sleep(.2)
if not observed:raise RuntimeError('未观察到真实睡眠子进程')
blocked=m.submit('queued-same','claude','Reply exactly SHOULD_NOT_RUN.',p,120,'text_only')
unaffected=m.submit('queued-safe','claude','Reply exactly QUEUE_RECOVERY_PASS.',safe,120,'text_only')
states=[m.c.call('get_task',{'task_id':x['id']})['state'] for x in (blocked,unaffected)]
pid=m.c.request('/health')['pid'];os.kill(pid,signal.SIGKILL);time.sleep(.5)
m.c.ensure()
r1=m.c.call('get_task',{'task_id':t['id']})
end=time.monotonic()+5
while time.monotonic()<end:
 r2=m.c.call('get_task',{'task_id':blocked['id']})
 if r2['state']=='interrupted':break
 time.sleep(.1)
r3=m.wait(unaffected,160)
for x in observed:
 current=next((r for r in m.processes() if r['pid']==x['pid']),None)
 if current and current['command']==x['command']:
  try:os.kill(x['pid'],signal.SIGKILL)
  except ProcessLookupError:pass
m.save('queue_recovery',dict(before_crash=states,running=r1,blocked=r2,safe=r3,passed=states==['queued','queued'] and r1['state']=='interrupted' and r2['state']=='interrupted' and r2.get('workspace_quarantined') and r3['state']=='succeeded' and 'QUEUE_RECOVERY_PASS' in r3.get('answer','')))
m.c.stop()
