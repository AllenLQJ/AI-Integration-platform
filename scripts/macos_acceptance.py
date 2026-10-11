"""Mac 实机验收；独立私有后台，证据保留，不停止共享 Hub。"""
import json, os, signal, subprocess, time
from pathlib import Path
from agent_hub.client import Client
from agent_hub.config import data_dir, private_dir

ROOT = data_dir() / 'mac-full-acceptance-20261011'
private_dir(ROOT)
config = json.loads((data_dir() / 'config.json').read_text())
(ROOT / 'config.json').write_text(json.dumps(config))
(ROOT / 'config.json').chmod(0o600)
c = Client(ROOT)
results = {}
def save(name, value):
    results[name] = value
    (ROOT / 'summary.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(name, json.dumps(value, ensure_ascii=False), flush=True)
def repo(name):
    p = ROOT / name
    if not p.exists():
        p.mkdir()
        subprocess.run(['git','init','-q',str(p)],check=True)
        (p/'hello.txt').write_text('BASELINE\n')
        subprocess.run(['git','-C',str(p),'add','.'],check=True)
        subprocess.run(['git','-C',str(p),'-c','user.name=Acceptance','-c','user.email=acceptance@localhost','commit','-qm','验收基线'],check=True)
    return p
def submit(key, agent, prompt, path, timeout=180, mode='read_only'):
    return c.call('submit_task',dict(request_id='mac-full-'+key,agent=agent,prompt=prompt,cwd=str(path),mode=mode,timeout_seconds=timeout))
def wait(task, seconds=240):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        r=c.call('get_result',{'task_id':task['id']})
        if r['ready']: return r
        time.sleep(.3)
    raise RuntimeError('验收等待期限耗尽 '+task['id'])
def processes():
    rows=[]
    for line in subprocess.check_output(['ps','-axo','pid=,ppid=,pgid=,command='],text=True).splitlines():
        parts=line.strip().split(None,3)
        if len(parts)==4: rows.append(dict(pid=int(parts[0]),ppid=int(parts[1]),pgid=int(parts[2]),command=parts[3]))
    return rows
def descendants(pid, rows):
    ids={pid}
    for _ in range(30):
        found={r['pid'] for r in rows if r['ppid'] in ids}
        if found<=ids: break
        ids|=found
    return [r for r in rows if r['pid'] in ids]

def main():
    save('agents',c.call('list_agents'))
    for kind in ('cancel','timeout'):
        p=repo('生命周期 '+kind)
        t=submit(kind,'codex','Run exactly one shell command: /bin/sleep 90 . This authorized diagnostic only sleeps. Wait normally; an external supervisor will stop it. Do not replace execution with explanation.',p,120 if kind=='cancel' else 25)
        observed=[]; end=time.monotonic()+110
        while time.monotonic()<end:
            events=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
            roots=[e['data']['pid'] for e in events if e['kind']=='process']
            if roots:
                tree=descendants(roots[0],processes())
                if any('/bin/sleep 90' in r['command'] and r['pid']!=roots[0] for r in tree):
                    observed=tree; break
            if c.call('get_result',{'task_id':t['id']})['ready']: break
            time.sleep(.2)
        started=time.monotonic()
        if kind=='cancel': c.call('cancel_task',{'task_id':t['id']})
        r=wait(t,140);time.sleep(.5)
        live={x['pid'] for x in processes()}
        remaining=[x for x in observed if x['pid'] in live]
        save(kind,dict(task_id=t['id'],result=r,observed=observed,remaining=remaining,elapsed=round(time.monotonic()-started,2),passed=bool(observed and not remaining and r['state']==('cancelled' if kind=='cancel' else 'timed_out'))))
    p=repo('Muse 网页验收')
    t=submit('muse-web','muse','请实际浏览 https://www.sqlite.org/lang_transaction.html 和 https://www.sqlite.org/isolation.html ，比较 DEFERRED、IMMEDIATE、EXCLUSIVE 与 WAL 下读写并发。打开两个网页并核对具体段落，给出三条简明结论和两个来源链接。只进行网页阅读，不写文件、不登录、不发消息。',p,240,'delegate')
    r=wait(t,280)
    save('muse_web',dict(task_id=t['id'],result=r,passed=r['state']=='succeeded' and len([u for u in r.get('source_urls',[]) if u.startswith('https://www.sqlite.org/')])>=2))
    save('boundaries',dict(remote_cancel='Muse adapter.remote_cancel=False; Hub 不提供远端停止能力',cross_machine='HTTP 服务绑定 127.0.0.1；客户端拒绝非回环地址；当前版本无跨机器 Hub 调度接口'))

if __name__=='__main__': main()
