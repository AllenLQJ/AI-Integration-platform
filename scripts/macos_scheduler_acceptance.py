"""真实双执行器并发与同工作区串行验收。"""
import json,time
from macos_acceptance import ROOT,c,repo,submit,wait
from agent_hub.store import Store
summary={}
for scenario in ('parallel','serial'):
    a=repo('并发 A '+scenario);b=repo('并发 B '+scenario) if scenario=='parallel' else a
    prompt='This is a scheduler diagnostic. Produce a detailed explanation of transaction isolation, including at least 20 examples. Do not use tools or change files.'
    t1=submit(scenario+'-codex','codex',prompt,a,120)
    t2=submit(scenario+'-claude','claude',prompt,b,120,'text_only')
    snapshots=[];overlap=False;end=time.monotonic()+140
    while time.monotonic()<end:
        states=[c.call('get_task',{'task_id':t['id']})['state'] for t in (t1,t2)]
        if not snapshots or snapshots[-1]['states']!=states: snapshots.append(dict(time=time.time(),states=states))
        if states==['running','running']:overlap=True
        if all(x in {'succeeded','failed','cancelled','timed_out'} for x in states):break
        time.sleep(.1)
    r=[wait(t) for t in (t1,t2)]
    summary[scenario]=dict(task_ids=[t1['id'],t2['id']],snapshots=snapshots,overlap=overlap,results=r,passed=all(x['state']=='succeeded' for x in r) and overlap==(scenario=='parallel'))
    (ROOT/'scheduler.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(scenario,summary[scenario]['passed'],snapshots,flush=True)
