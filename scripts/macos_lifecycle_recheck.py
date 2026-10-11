"""修复后的 Mac 进程树真实复验；独立后台。"""
import json,time
import macos_acceptance as m
from agent_hub.client import Client
from agent_hub.config import private_dir,data_dir
m.ROOT=data_dir()/'mac-lifecycle-recheck-20261011'
private_dir(m.ROOT)
(m.ROOT/'config.json').write_text((data_dir()/'config.json').read_text());(m.ROOT/'config.json').chmod(0o600)
m.c=Client(m.ROOT)
for kind in ('cancel','timeout'):
    p=m.repo(kind)
    t=m.submit(kind,'codex','Run exactly /bin/sleep 90 as one shell command. This authorized cancellation diagnostic only sleeps; wait normally. Do not explain instead of executing.',p,120 if kind=='cancel' else 35)
    observed=[];end=time.monotonic()+110
    while time.monotonic()<end:
        events=m.c.call('get_events',{'task_id':t['id'],'limit':200})['events']
        roots=[e['data']['pid'] for e in events if e['kind']=='process']
        if roots:
            tree=m.descendants(roots[0],m.processes())
            if any(r['command']=='/bin/sleep 90' for r in tree):observed=tree;break
        if m.c.call('get_result',{'task_id':t['id']})['ready']:break
        time.sleep(.2)
    start=time.monotonic()
    if kind=='cancel':m.c.call('cancel_task',{'task_id':t['id']})
    result=m.wait(t,140)
    time.sleep(.5)
    live={r['pid'] for r in m.processes()};remaining=[r for r in observed if r['pid'] in live]
    m.save(kind,dict(task_id=t['id'],result=result,observed=observed,remaining=remaining,elapsed=round(time.monotonic()-start,2),passed=bool(observed and not remaining and result['state']==('cancelled' if kind=='cancel' else 'timed_out'))))
m.c.stop()
