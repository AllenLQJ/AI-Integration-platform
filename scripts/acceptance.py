"""Explicit real-account acceptance. Uses only a private disposable Git repository."""
import json
import subprocess
import time
import uuid
from pathlib import Path
from agent_hub.client import Client
from agent_hub.config import data_dir


def wait(client, task):
    ident = task['id']
    last = None
    deadline = time.time() + 240
    while time.time() < deadline:
        current = client.call('get_task', {'task_id': ident})
        if current['state'] != last:
            print(json.dumps({'task': ident, 'agent': current['agent'], 'state': current['state']}), flush=True)
            last = current['state']
        result = client.call('get_result', {'task_id': ident})
        if result['ready']: return result
        time.sleep(2)
    client.call('cancel_task', {'task_id': ident})
    return {'state': 'acceptance_timeout'}


def main():
    root = data_dir()
    client = Client(root)
    stamp = uuid.uuid4().hex[:8]
    repo = root / 'acceptance' / ('验证 中文 space ' + stamp)
    repo.mkdir(parents=True)
    def git(*args):
        subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
    git('init'); git('config', 'user.name', 'Hub Acceptance'); git('config', 'user.email', 'test@localhost')
    (repo / 'hello.txt').write_text('base\n', encoding='utf-8')
    git('add', '.'); git('commit', '-m', 'baseline')
    results = {}
    for agent in ('codex', 'claude'):
        marker = 'HUB_' + stamp + '_' + agent
        args = dict(request_id='accept-' + stamp + agent, agent=agent, mode='edit_worktree',
                    cwd=str(repo), timeout_seconds=180,
                    prompt='Edit only hello.txt in the current working directory: replace its content with '
                           + marker + '\n. Do not run unrelated tools. Then reply exactly ' + marker)
        first = client.call('submit_task', args)
        duplicate = client.call('submit_task', args)
        result = wait(client, first)
        entry = {'task_id': first['id'], 'duplicate_same': first['id'] == duplicate['id'],
                 'first': result, 'main_unchanged': (repo / 'hello.txt').read_text() == 'base\n'}
        workspace = result.get('artifacts', {}).get('workspace')
        entry['edit_verified'] = bool(workspace and (Path(workspace)/'hello.txt').read_text(encoding='utf-8').strip() == marker)
        if result.get('state') == 'succeeded':
            follow = client.call('continue_task', dict(request_id='follow-' + stamp + agent,
                task_id=first['id'], prompt='What exact marker did I ask you to write? Reply only that marker. Do not use tools.',
                timeout_seconds=180))
            entry['follow'] = wait(client, follow)
            entry['resume_verified'] = entry['follow'].get('answer', '').strip() == marker
        results[agent] = entry
        (root / 'acceptance-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'agent': agent, 'state': result.get('state'), 'answer': result.get('answer'),
                          'error': result.get('error'), 'main_unchanged': entry['main_unchanged']}, ensure_ascii=False), flush=True)
    first = client.call('submit_task', dict(request_id='accept-' + stamp + 'muse', agent='muse',
            prompt='用两句话说明 SQLite 事务的用途，提供 SQLite 官方文档链接。记住本轮编号 '+stamp+'，无需长调研。',
            cwd=str(repo), mode='read_only', timeout_seconds=180))
    result = wait(client, first)
    entry = {'task_id': first['id'], 'first': result}
    if result.get('state') == 'succeeded':
        follow = client.call('continue_task', dict(request_id='follow-' + stamp + 'muse', task_id=first['id'],
                       prompt='上一轮我要求你记住的编号是什么？只回答编号。', timeout_seconds=180))
        entry['follow'] = wait(client, follow)
    results['muse'] = entry
    (root / 'acceptance-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'muse_state': result.get('state'), 'answer': result.get('answer'), 'error': result.get('error')}, ensure_ascii=False), flush=True)

if __name__ == '__main__': main()
