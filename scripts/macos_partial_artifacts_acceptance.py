"""真实 worktree 编辑后超时，保留部分差异和日志，原目录不修改。"""
import json
import macos_acceptance as m
from agent_hub.client import Client
from agent_hub.config import private_dir,data_dir
m.ROOT=data_dir()/'mac-partial-artifacts-20261011';private_dir(m.ROOT)
(m.ROOT/'config.json').write_text((data_dir()/'config.json').read_text());(m.ROOT/'config.json').chmod(0o600)
m.c=Client(m.ROOT);p=m.repo('部分产物 中文')
t=m.submit('partial','codex','In this dedicated test worktree, change hello.txt to exactly MAC_PARTIAL_PASS followed by a newline. Then execute /bin/sleep 90 and wait. This authorized timeout diagnostic must edit first then sleep. Do not commit or touch any other file.',p,35,'edit_worktree')
r=m.wait(t,90)
art=r.get('artifacts',{});task=m.c.call('get_task',{'task_id':t['id']})
m.save('partial',dict(task_id=t['id'],result=r,original=(p/'hello.txt').read_text(),workspace=task.get('workspace'),passed=r['state']=='timed_out' and (p/'hello.txt').read_text()=='BASELINE\n' and 'MAC_PARTIAL_PASS' in art.get('diff','') and bool(art.get('logs'))))
m.c.stop()
