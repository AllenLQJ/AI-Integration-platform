"""仅暂停独立 Hub 的真实进程，验证期限跨越后的恢复；不等同整机休眠。"""
import os,signal,subprocess,time
from macos_resilience_common import make_client,repo,save,submit,wait
c,root=make_client('daemon-pause');p=repo('独立后台 暂停')
pid=None;paused=False
try:
 c.ensure();pid=c.request('/health')['pid']
 t=submit(c,'pause-active','codex','Run exactly /bin/sleep 90 now; authorized cancellation diagnostic only sleeps. Do not write files.',p,20,'read_only')
 end=time.monotonic()+16;observed=False
 while time.monotonic()<end:
  ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
  if any(e['kind']=='progress' and e['data'].get('item_type')=='command_execution' for e in ev):observed=True;break
  time.sleep(.1)
 if not observed:raise RuntimeError('未观测到真实工具启动，不暂停后台')
 queued=submit(c,'pause-queued','claude','Reply exactly PAUSE_QUEUE_PASS. Do not use tools.',p,90,'text_only')
 before=[c.call('get_task',{'task_id':x['id']})['state'] for x in (t,queued)]
 ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
 started=min(e['timestamp'] for e in ev if e['kind']=='state' and e['data'].get('state')=='running')
 deadline=started+20
 # 暂停至原期限之后 2 秒；只暂停本探针拥有的后台 PID。
 duration=max(4,deadline-time.time()+2)
 os.kill(pid,signal.SIGSTOP);paused=True;began=time.time();time.sleep(duration);os.kill(pid,signal.SIGCONT);paused=False
 resumed=time.time();r1=wait(c,t,45);r2=wait(c,queued,115);health=c.request('/health')
 save('daemon-pause-summary',dict(passed=before==['running','queued'] and resumed>deadline and r1['state']=='timed_out' and r2['state']=='succeeded' and 'PAUSE_QUEUE_PASS' in r2.get('answer','') and health['pid']==pid,before=before,pause_seconds=round(resumed-began,2),deadline=deadline,resumed_at=resumed,results=[r1,r2],health=health,limitation='SIGSTOP/SIGCONT 仅暂停 Hub，不涉及整机电源或网络；不能替代真实跨期限休眠。'))
finally:
 if paused and pid:os.kill(pid,signal.SIGCONT)
 c.stop()
