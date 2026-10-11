"""已获用户授权的真实 Mac 休眠与唤醒验收；不修改电源设置。"""
import datetime,json,re,subprocess,time,sys
from pathlib import Path
from macos_resilience_common import ROOT,make_client,repo,save,submit,wait,write_json
from agent_hub.client import Client
from agent_hub.config import data_dir
from agent_hub.store import Store
from macos_sleep_evidence import evaluate_power_log,full_wake_rows,timeout_processed_after_resume

# 睡眠前先确保共享 Hub 没有在途任务。
if Store(data_dir()/'tasks.db').list({'running','queued','cancelling'}):
 raise SystemExit('共享后台有任务，暂不进行休眠')
recheck='--recheck' in sys.argv or '--final-recheck' in sys.argv
label='sleep-resume-final' if '--final-recheck' in sys.argv else 'sleep-resume-recheck' if recheck else 'sleep-resume'
c,root=make_client(label);p=repo('休眠 唤醒 复验' if recheck else '休眠 唤醒')
try:
 c.ensure();pid=c.request('/health')['pid']
 t=submit(c,'sleep-active','codex','Run exactly /bin/sleep 120 now. This authorized sleep/timeout diagnostic only sleeps; wait normally. Do not write files.',p,20,'read_only')
 observed=False;end=time.monotonic()+18
 while time.monotonic()<end:
  ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
  roots=[e['data']['pid'] for e in ev if e['kind']=='process']
  if roots:
   output=subprocess.check_output(['ps','-axo','pid=,ppid=,command='],text=True)
   rows=[line.strip().split(None,2) for line in output.splitlines()]
   children=set(roots)
   for _ in range(30):
    found={int(row[0]) for row in rows if len(row)==3 and int(row[1]) in children}
    if found<=children:break
    children|=found
   tracked=[int(row[0]) for row in rows if len(row)==3 and int(row[0]) in children]
   if any(len(row)==3 and int(row[0]) in children and row[2]=='/bin/sleep 120' for row in rows):observed=True;break
  if c.call('get_result',{'task_id':t['id']})['ready']:break
  time.sleep(.1)
 if not observed:raise RuntimeError('20 秒期限内未观察到验收 sleep 子进程，不进入休眠')
 queued=submit(c,'sleep-queued','claude','Reply exactly WAKE_QUEUE_PASS. Do not use tools.',p,90,'text_only')
 before=[c.call('get_task',{'task_id':x['id']})['state'] for x in (t,queued)]
 if before!=['running','queued']:raise RuntimeError('休眠前任务状态不符合预期 '+str(before))
 ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
 started=min(e['timestamp'] for e in ev if e['kind']=='state' and e['data'].get('state')=='running')
 if recheck:
  # 即使几秒后因输入设备唤醒，也能覆盖任务期限跨越。
  time.sleep(max(0,started+20-time.time()-6))
 baseline=dict(time=time.time(),monotonic=time.monotonic(),daemon_pid=pid,task_id=t['id'],queued_id=queued['id'],deadline=started+20,states=before,observed_pids=tracked)
 write_json(ROOT/(label+'-baseline.json'),baseline)
 print('READY_FOR_AUTHORIZED_SLEEP',flush=True)
 command=subprocess.run(['pmset','sleepnow'],capture_output=True,text=True,timeout=10)
 # 等待一次真正的 Sleep/Wake 记录；执行环境随整机暂停，唤醒后继续。
 pm_log='';sleep_rows=[];wake_rows=[];deadline=time.monotonic()+90
 while time.monotonic()<deadline:
  pm_log=subprocess.check_output(['pmset','-g','log'],text=True)
  recent=[]
  for line in pm_log.splitlines():
   match=re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})',line)
   if match:
    try:stamp=datetime.datetime.strptime(match[1],'%Y-%m-%d %H:%M:%S %z').timestamp()
    except ValueError:continue
    if stamp>=baseline['time']-1:recent.append(line)
  sleep_rows=[line for line in recent if 'Entering Sleep state' in line]
  wake_rows=full_wake_rows(recent)
  if sleep_rows and wake_rows:break
  time.sleep(1)
 resumed=time.time()
 cycle=evaluate_power_log(recent,baseline['deadline'])
 crossed=cycle['deadline_crossed']
 r1=wait(c,t,45);r2=wait(c,queued,115)
 health=c.request('/health')
 final=submit(c,'sleep-new-task','codex','Reply exactly WAKE_NEW_TASK_PASS. Do not use tools.',repo('唤醒 新任务'),90,'read_only')
 r3=wait(c,final,115)
 state_events=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
 timed_out_at=next((e['timestamp'] for e in state_events if e['kind']=='state' and e['data'].get('state')=='timed_out'),None)
 after_resume=timeout_processed_after_resume(cycle,timed_out_at)
 shared=Client(data_dir()).request('/health')
 (ROOT/(label+'-power-events.log')).write_text('\n'.join(recent));(ROOT/(label+'-power-events.log')).chmod(0o600)
 basic=command.returncode==0 and bool(sleep_rows and wake_rows) and health['pid']==pid and r1['state']=='timed_out' and r2['state']=='succeeded' and 'WAKE_QUEUE_PASS' in r2.get('answer','') and r3['state']=='succeeded' and 'WAKE_NEW_TASK_PASS' in r3.get('answer','')
 save(label+'-summary',dict(passed=basic and crossed and after_resume,basic_resume_passed=basic,timed_out_at=timed_out_at,timeout_processed_after_resume=after_resume,sleep_command_exit=command.returncode,sleep_command_error=command.stderr[:500],before=baseline,resumed_at=resumed,wall_interval_seconds=round(resumed-baseline['time'],2),**cycle,sleep_events=sleep_rows,wake_events=wake_rows,daemon_after=health,shared_after=shared,results=[r1,r2,r3],limitation='一次手动唤醒周期，不能代替不同电源状态和数小时休眠的测试。'))
finally:c.stop()
