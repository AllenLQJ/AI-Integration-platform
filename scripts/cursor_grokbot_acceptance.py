"""独立私有目录中的真实执行器验收；会消耗已有账号额度。"""
import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from agent_hub.config import private_dir
from agent_hub.client import Client


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, help='新的私有验收目录，禁止复用旧结果')
    parser.add_argument('--agent', choices=['cursor', 'grokbot', 'both'], default='both')
    parser.add_argument('--https-proxy', default=os.environ.get('HTTPS_PROXY'), help='可选网络代理，仅用于执行器')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if root.exists():
        parser.error('验收目录已存在，请指定新路径')
    private_dir(root)
    agents = {}
    env = {'HTTPS_PROXY':args.https_proxy, 'HTTP_PROXY':args.https_proxy} if args.https_proxy else {}
    if args.agent in ('cursor','both'):
        agents['cursor'] = {'executable':str(Path.home()/'.local/bin/agent'),
                            'env':env}
    if args.agent in ('grokbot','both'):
        agents['grokbot'] = {'bot_name':'Agent Hub Worker',
                             'env':env}
    save(root/'config.json', {'agents':agents})
    client = Client(root)
    tasks = {}
    nonce = 'HUB-PROBE-' + uuid.uuid4().hex[:12]
    for agent in agents:
        repo = root/('repo-'+agent)
        private_dir(repo)
        subprocess.run(['git','init','-q',str(repo)],check=True)
        (repo/'probe.txt').write_text(nonce+'\n',encoding='utf-8')
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=验收','-c','user.email=probe@localhost',
                        'commit','-qm','验收基线'],check=True)
        prompt = ('读取当前工作区 probe.txt，原样回复文件中的唯一标记。不要改文件，不运行命令。'
                  if agent == 'cursor' else '计算 37+58，回复计算结果与本条随机标记 '+nonce+'。不要使用外部工具。')
        tasks[agent] = client.call('submit_task', dict(request_id=uuid.uuid4().hex,agent=agent,prompt=prompt,
            cwd=str(repo),mode='read_only' if agent == 'cursor' else 'delegate',timeout_seconds=120))
    report = {'nonce':nonce,'tasks':tasks,'results':{}}
    save(root/'report.json',report)
    end = time.monotonic()+140
    try:
        while time.monotonic()<end and len(report['results'])<len(tasks):
            for agent, task in tasks.items():
                if agent in report['results']:
                    continue
                result = client.call('get_result',{'task_id':task['id']})
                if result['ready']:
                    passed = result['state']=='succeeded' and nonce in result.get('answer','')
                    if agent == 'grokbot':
                        passed = passed and '95' in result.get('answer','')
                    report['results'][agent] = dict(result, content_verified=passed)
                    save(root/'report.json',report)
                    print(agent,json.dumps({'state':result['state'],'content_verified':passed,
                                           'error':result.get('error'),'answer':result.get('answer')},ensure_ascii=False),flush=True)
            time.sleep(.5)
    finally:
        client.stop()
    if len(report['results'])!=len(tasks) or not all(r['content_verified'] for r in report['results'].values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
