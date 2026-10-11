"""补充验收公共方法；独立私有目录，不覆盖已有验收。"""
import hashlib,json,os,subprocess,time
from pathlib import Path
from agent_hub.client import Client
from agent_hub.config import data_dir,private_dir

ROOT=data_dir()/'mac-resilience-20261011'
private_dir(ROOT)
BASE=json.loads((data_dir()/'config.json').read_text())

def write_json(path,value):
    path=Path(path);private_dir(path.parent)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)

def save(name,value):
    write_json(ROOT/(name+'.json'),value)
    print(name,json.dumps({k:v for k,v in value.items() if k in ('passed','state','cases','limitation')},ensure_ascii=False),flush=True)

def make_client(name,config=None):
    root=ROOT/name;private_dir(root);write_json(root/'config.json',config or BASE)
    return Client(root),root

def repo(name):
    p=ROOT/'repos'/name
    if not p.exists():
        private_dir(p)
        subprocess.run(['git','init','-q',str(p)],check=True)
        (p/'probe.txt').write_text('BASELINE\n')
        subprocess.run(['git','-C',str(p),'add','.'],check=True)
        subprocess.run(['git','-C',str(p),'-c','user.name=验收','-c','user.email=probe@localhost','commit','-qm','验收基线'],check=True)
    return p

def submit(c,key,agent,prompt,path,timeout=90,mode='text_only'):
    return c.call('submit_task',dict(request_id=key,agent=agent,prompt=prompt,cwd=str(path),mode=mode,timeout_seconds=timeout))

def wait(c,t,seconds=120):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        r=c.call('get_result',{'task_id':t['id']})
        if r['ready']:return r
        time.sleep(.2)
    raise RuntimeError('任务等待超出探针期限 '+t['id'])

def checksum(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
