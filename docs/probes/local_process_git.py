"""Temporary Git/worktree and synthetic process-tree probes."""
import json, subprocess, sys, tempfile, time
from pathlib import Path

def git(cwd,*args):
    return subprocess.run(['git','-C',str(cwd),*args],capture_output=True,text=True,encoding='utf-8',check=True).stdout.strip()

def tree_test(root,kind):
    marker=root/(kind+'-child.pid')
    childcode='import time; time.sleep(120)'
    code="import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c',%r]); Path(%r).write_text(str(p.pid)); time.sleep(120)"%(childcode,str(marker))
    p=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for _ in range(100):
        if marker.exists(): break
        time.sleep(.05)
    child=int(marker.read_text())
    if kind=='timeout':
        try: p.wait(.5)
        except subprocess.TimeoutExpired: pass
    subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True,check=True)
    p.wait(5)
    check=subprocess.run(['powershell','-NoProfile','-Command',f'@(Get-Process -Id {p.pid},{child} -ErrorAction SilentlyContinue).Count'],capture_output=True,text=True)
    return {'parent_pid':p.pid,'child_pid':child,'remaining_count':int(check.stdout.strip()),'pass':check.stdout.strip()=='0'}

with tempfile.TemporaryDirectory(prefix='hub 工作区 space ') as td:
    root=Path(td); repo=root/'原仓库 repo';repo.mkdir()
    git(repo,'init');git(repo,'config','user.name','Hub Probe');git(repo,'config','user.email','probe@example.invalid')
    (repo/'example.txt').write_text('baseline\n',encoding='utf-8')
    git(repo,'add','example.txt');git(repo,'commit','-m','temporary probe baseline')
    base=git(repo,'rev-parse','HEAD'); wt=root/'任务 worktree'
    git(repo,'worktree','add','-b','probe-task',str(wt),base)
    (wt/'example.txt').write_text('edited in worktree\n',encoding='utf-8')
    result={'worktree':{'unicode_spaces':True,'base':base,'main_unchanged':(repo/'example.txt').read_text()=='baseline\n','main_status':git(repo,'status','--porcelain'),'worktree_diff_present':bool(git(wt,'diff','--','example.txt'))},'cancel':tree_test(root,'cancel'),'timeout_cleanup':tree_test(root,'timeout')}
    # Preserve deliberate changes as a commit before removing only this disposable worktree.
    git(wt,'add','example.txt');git(wt,'commit','-m','temporary worktree test change');git(repo,'worktree','remove',str(wt))
    out=Path(__file__).parent/'results'/'local.json';out.parent.mkdir(exist_ok=True);out.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
