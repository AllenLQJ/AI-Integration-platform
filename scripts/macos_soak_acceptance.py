"""五分钟、十批真实双执行器任务；持续采样健康、RSS、队列和结果。"""
import json,subprocess,time
from collections import Counter
from pathlib import Path
from macos_resilience_common import make_client,repo,save,submit,wait,write_json,ROOT
from agent_hub.store import Store
c,root=make_client('soak-5min')
start=time.monotonic();tasks=[];health=[];samples=[];results=[];passed=True
try:
 c.ensure();pid=c.request('/health')['pid']
 for batch in range(10):
  target=start+batch*30
  while time.monotonic()<target:
   health.append(dict(time=time.time(),health=c.request('/health')))
   states=Store(root/'tasks.db').list({'running','queued','cancelling'})
   rss=subprocess.check_output(['ps','-p',str(pid),'-o','rss='],text=True).strip()
   samples.append(dict(time=time.time(),rss_kib=int(rss or 0),active=len([t for t in states if t['state']=='running']),queued=len([t for t in states if t['state']=='queued'])))
   time.sleep(min(1,target-time.monotonic()) if target>time.monotonic() else .01)
  pair=[]
  for agent,mode in [('codex','read_only'),('claude','text_only')]:
   marker='SOAK_'+str(batch)+'_'+agent.upper();key='soak-'+str(batch)+'-'+agent
   path=repo('持续运行 '+agent+' '+str(batch))
   t=submit(c,key,agent,'Reply exactly '+marker+'. Do not use tools.',path,60,mode)
   replay=submit(c,key,agent,'Reply exactly '+marker+'. Do not use tools.',path,60,mode)
   passed &= replay['id']==t['id'];pair.append((t,marker));tasks.append(t['id'])
  # 前一批的结果不阻塞后一批采样；由 2/1 额度真实排队。
  for t,marker in pair:
   r=wait(c,t,85);passed &= r['state']=='succeeded' and marker in r.get('answer','')
   results.append(dict(task_id=t['id'],expected=marker,result=r))
  print('batch',batch+1,'passed',passed,'elapsed',round(time.monotonic()-start),flush=True)
  write_json(ROOT/'soak-progress.json',dict(completed_batches=batch+1,results=results,elapsed=time.monotonic()-start))
 end=start+300
 while time.monotonic()<end:
  health.append(dict(time=time.time(),health=c.request('/health')))
  rss=subprocess.check_output(['ps','-p',str(pid),'-o','rss='],text=True).strip();samples.append(dict(time=time.time(),rss_kib=int(rss or 0),active=0,queued=0))
  time.sleep(min(1,end-time.monotonic()) if end>time.monotonic() else .01)
 active=Store(root/'tasks.db').list({'running','queued','cancelling'})
 passed &= not active and len(set(tasks))==20 and all(x['health']['pid']==pid for x in health)
 save('soak-summary',dict(passed=passed,duration_seconds=round(time.monotonic()-start,2),real_tasks=len(tasks),unique_tasks=len(set(tasks)),daemon_pid=pid,health_samples=len(health),rss_min_kib=min(x['rss_kib'] for x in samples),rss_max_kib=max(x['rss_kib'] for x in samples),rss_final_kib=samples[-1]['rss_kib'],active_at_end=len(active),results=results,samples=samples,limitation='五分钟有限持续验收；不代表数小时、数天或任意负载稳定性。'))
finally:c.stop()
