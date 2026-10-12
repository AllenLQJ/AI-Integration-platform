"""三方本机验收：真实执行器、真实 Codex/Claude 总控；证据留在私有目录。"""
import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

from agent_hub.client import Client
from agent_hub.config import adapters, data_dir, private_dir
from agent_hub.processes import environment, run_process


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='新的私有证据目录')
    parser.add_argument('--executors', nargs='*', choices=['codex', 'claude', 'cursor'], default=['codex', 'claude', 'cursor'])
    parser.add_argument('--controllers', nargs='*', choices=['codex', 'claude'], default=['codex', 'claude'])
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if root.exists():
        parser.error('目录已存在，禁止复用旧验收')
    private_dir(root)
    original = json.loads((data_dir() / 'config.json').read_text())
    selected = {name: original['agents'][name] for name in ('codex', 'claude', 'cursor')}
    if selected['cursor'].get('kind') == 'ide':
        selected['cursor'] = original['agents']['cursor_cli']
    save(root / 'config.json', {'agents': selected})
    configured = adapters(root)
    client = Client(root)
    report = {'executors': {}, 'controllers': {}, 'cursor_ide': '未验收；CLI 结果不能代表 IDE'}
    names = ('list_agents', 'submit_task', 'wait_for_task', 'get_result')
    try:
        for name in args.executors:
            workspace = root / ('executor-' + name)
            private_dir(workspace)
            marker = 'HUB_LOCAL_' + uuid.uuid4().hex
            (workspace / 'probe.txt').write_text(marker)
            task = client.call('submit_task', dict(request_id=uuid.uuid4().hex, agent=name,
                cwd=str(workspace), mode='read_only', timeout_seconds=180,
                prompt='读取工作目录 probe.txt，仅原样回复完整内容。不要写文件；允许只读 cat 命令。'))
            while True:
                snapshot = client.call('wait_for_task', dict(task_id=task['id'], timeout_seconds=10))
                result = client.call('get_result', dict(task_id=task['id']))
                if result['ready'] or snapshot.get('needs_user'):
                    break
            passed = result['ready'] and result['state'] == 'succeeded' and marker in result.get('answer', '')
            report['executors'][name] = dict(task_id=task['id'], passed=passed, result=result)
            save(root / 'report.json', report)
            print(json.dumps({'executor': name, 'passed': passed, 'state': result['state']}, ensure_ascii=False), flush=True)

        for name, target in [('codex', 'claude'), ('claude', 'codex')]:
            if name not in args.controllers:
                continue
            workspace = root / ('controller-' + name)
            private_dir(workspace)
            request_id = uuid.uuid4().hex
            marker = 'HUB_CONTROL_' + uuid.uuid4().hex
            prompt = ('只使用 agent_hub MCP 工具。先 list_agents，再只提交一次 submit_task：'
                + json.dumps(dict(request_id=request_id, agent=target, cwd=str(workspace),
                    mode='read_only', timeout_seconds=180, prompt='仅回复 ' + marker + '，不要使用工具。'), ensure_ascii=False)
                + '。保存任务 id，循环 wait_for_task，timeout_seconds=10，使用返回的 next_seq 续读，直到终态。'
                '再 get_result，回复任务 id 和完整 answer。禁止其他工具。')
            mcp = {'command': sys.executable, 'args': ['-m', 'agent_hub', 'mcp'],
                   'env': {'AGENT_HUB_DATA_DIR': str(root)}}
            env = environment(configured[name].env)
            env['ENABLE_TOOL_SEARCH'] = 'false'
            if name == 'codex':
                argv = [configured[name].executable, 'exec', '--ignore-user-config', '--ignore-rules',
                    '--skip-git-repo-check', '--json', '--sandbox', 'read-only',
                    '-c', 'approval_policy="never"', '-c', 'features.apps=false']
                for key, value in mcp.items():
                    if key == 'env':
                        argv += ['-c', 'mcp_servers.agent_hub.env.AGENT_HUB_DATA_DIR=' + json.dumps(str(root))]
                    else:
                        argv += ['-c', 'mcp_servers.agent_hub.' + key + '=' + json.dumps(value)]
                for tool in names:
                    argv += ['-c', 'mcp_servers.agent_hub.tools.' + tool + '.approval_mode="approve"']
                argv += [prompt]
            else:
                config = workspace / 'controller-mcp.json'
                save(config, {'mcpServers': {'agent_hub': mcp}})
                argv = [configured[name].executable, '-p', '--output-format', 'stream-json', '--verbose',
                    '--tools=', '--strict-mcp-config', '--mcp-config=' + str(config),
                    '--setting-sources=user', '--settings', '{"disableAllHooks":true}',
                    '--permission-mode', 'dontAsk',
                    '--allowedTools=' + ','.join('mcp__agent_hub__' + tool for tool in names), '--', prompt]
            rc, out, err, logs = run_process(argv, workspace, env, threading.Event(),
                time.time() + 300, workspace / 'logs', lambda *args: None)
            # 从实际 Hub 记录核对提交和结果，不能仅凭模型口述通过。
            from agent_hub.store import Store
            store = Store(root / 'tasks.db')
            with store.connection() as db:
                row = db.execute('SELECT id FROM tasks WHERE request_id=?', (request_id,)).fetchone()
            result = client.call('get_result', {'task_id': row[0]}) if row else None
            called_result = 'get_result' in out and 'submit_task' in out
            passed = bool(rc == 0 and called_result and result and result['ready']
                          and result['state'] == 'succeeded' and marker in result.get('answer', '') and marker in out)
            report['controllers'][name] = dict(target=target, passed=passed, exit_code=rc,
                                               result=result, logs=logs)
            save(root / 'report.json', report)
            print(json.dumps({'controller': name, 'target': target, 'passed': passed}, ensure_ascii=False), flush=True)
    finally:
        client.stop()
    print('证据：' + str(root / 'report.json'), flush=True)
    if not all(item['passed'] for group in ('executors', 'controllers') for item in report[group].values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
