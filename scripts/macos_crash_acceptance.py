"""独立后台真实崩溃与恢复隔离；仅终止本脚本后台和已观测进程。"""
import json,os,signal,time
import macos_acceptance as m
from agent_hub.client import Client
from agent_hub.config import private_dir,data_dir
from agent_hub.models import HubError
m.ROOT=data_dir()/'mac-crash-acceptance-20261011';private_dir(m.ROOT)
(m.ROOT/'config.json').write_text((data_dir()/'config.json').read_text());(m.ROOT/'config.json').chmod(0o600)
m.c=Client(m.ROOT)
p=m.repo('崩溃 工作区')
t=m.submit('crash','codex','Run /bin/sleep 90 now. This authorized crash diagnostic only sleeps. Do not modify files.',p,180)
observed=[];end=time.monotonic()+110
while time.monotonic()<end:
    events=m.c.call('get_events',{'task_id':t['id'],'limit':200})['events']
    roots=[e['data']['pid'] for e in events if e['kind']=='process']
    if roots:
        tree=m.descendants(roots[0],m.processes())
        if any(r['command']=='/bin/sleep 90' for r in tree):observed=tree;break
    if m.c.call('get_result',{'task_id':t['id']})['ready']:break
    time.sleep(.2)
if not observed:raise RuntimeError('真实子进程尚未启动，不能作为崩溃证据')
pid=m.c.request('/health')['pid'];os.kill(pid,signal.SIGKILL);time.sleep(.8)
remaining=[r for r in observed if r['pid'] in {x['pid'] for x in m.processes()}]
r=m.c.call('get_task',{'task_id':t['id']})
replay=m.c.call('submit_task',dict(request_id='mac-full-crash',agent='codex',prompt='Run /bin/sleep 90 now. This authorized crash diagnostic only sleeps. Do not modify files.',cwd=str(p),mode='read_only',timeout_seconds=180))
blocked={}
for name,tool,args in [('new','submit_task',dict(request_id='mac-crash-new',agent='codex',prompt='Reply PASS',cwd=str(p),mode='read_only',timeout_seconds=30)),('continue','continue_task',dict(request_id='mac-crash-follow',task_id=t['id'],prompt='Reply PASS',timeout_seconds=30))]:
    try:m.c.call(tool,args);blocked[name]='NOT_BLOCKED'
    except HubError as e:blocked[name]=e.code
for item in remaining:
    current=next((x for x in m.processes() if x['pid']==item['pid']),None)
    if current and current['command']==item['command']:
        try:os.kill(item['pid'],signal.SIGKILL)
        except ProcessLookupError:pass
m.save('crash',dict(task_id=t['id'],daemon_pid=pid,observed=observed,survived_daemon_crash=remaining,recovered_task=r,replay_same_id=replay['id']==t['id'],blocked=blocked,passed=r['state']=='interrupted' and r.get('workspace_quarantined') and replay['id']==t['id'] and set(blocked.values())=={'WORKSPACE_UNCERTAIN'},limitation='SIGKILL 后 CLI 可能继续；恢复时隔离且不自动重发。本轮仅清理已核对的验收进程。'))
m.c.stop()
