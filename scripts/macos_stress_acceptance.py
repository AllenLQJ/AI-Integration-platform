"""六个真实任务的并发额度、排队、幂等验收。"""
import json,time
from collections import Counter
from macos_acceptance import ROOT,c,repo,submit,wait
items=[]
for i in range(6):
 agent='codex' if i%2==0 else 'claude'
 path=repo('压力 '+str(i))
 t=submit('stress-'+str(i),agent,'Reply exactly MAC_STRESS_'+str(i)+'. Do not use tools.',path,120,'read_only' if agent=='codex' else 'text_only')
 replay=submit('stress-'+str(i),agent,'Reply exactly MAC_STRESS_'+str(i)+'. Do not use tools.',path,120,'read_only' if agent=='codex' else 'text_only')
 items.append((t,replay['id']==t['id']))
snapshots=[];maxactive=0;valid=True;end=time.monotonic()+260
while time.monotonic()<end:
 tasks=[c.call('get_task',{'task_id':t['id']}) for t,_ in items]
 states=[t['state'] for t in tasks]
 active=[t for t in tasks if t['state']=='running'];maxactive=max(maxactive,len(active))
 valid &= len(active)<=2 and all(n<=1 for n in Counter(t['agent'] for t in active).values())
 if not snapshots or snapshots[-1]['states']!=states:snapshots.append(dict(time=time.time(),states=states))
 if all(t['state'] in {'succeeded','failed','timed_out','cancelled'} for t in tasks):break
 time.sleep(.15)
results=[wait(t) for t,_ in items]
summary=dict(task_ids=[t['id'] for t,_ in items],snapshots=snapshots,max_active=maxactive,limits_respected=valid,idempotent=all(s for _,s in items),results=results,passed=valid and maxactive==2 and all(s for _,s in items) and all(r['state']=='succeeded' and 'MAC_STRESS_'+str(i) in r.get('answer','') for i,r in enumerate(results)))
(ROOT/'stress.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k not in ('results','snapshots')},ensure_ascii=False),flush=True)
