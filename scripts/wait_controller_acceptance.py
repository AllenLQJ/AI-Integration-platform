"""Real controllers, isolated HTTP/MCP, deterministic executor; no shared daemon restart."""
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_hub.config import adapters, data_dir, private_dir
from agent_hub.daemon import make_server
from agent_hub.processes import environment, run_process
from agent_hub.service import Service
from agent_hub.store import Store


class Fixture:
    capabilities = ['read_only']
    remote_cancel = True

    def __init__(self):
        self.wait_started = threading.Event()

    def diagnose(self):
        return {'available': True, 'capabilities': self.capabilities}

    def run(self, task, cancel, event):
        while not self.wait_started.wait(.05):
            if cancel.is_set():
                return {'answer': ''}
        cancel.wait(.65)
        return {'answer': 'WAIT_HUB_PASS', 'session_id': 'fixture'}


class RecordedService(Service):
    def __init__(self, root, fixture):
        self.records = []
        self.fixture = fixture
        super().__init__(root, {'fixture': fixture})

    def call(self, tool, arguments):
        if tool == 'wait_for_task':
            self.fixture.wait_started.set()
        started = time.monotonic()
        result = super().call(tool, arguments)
        self.records.append(Store.safe(dict(tool=tool, arguments=arguments,
            result=result, elapsed_seconds=time.monotonic() - started)))
        return result


def main():
    configured = adapters(data_dir())
    evidence = data_dir() / ('wait-controller-acceptance-' +
        datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S'))
    private_dir(evidence)
    summary = {}
    for name in ('codex', 'claude'):
        if name not in configured:
            raise RuntimeError(name + ' controller not configured')
        root = evidence / name
        private_dir(root)
        fixture = Fixture()
        service = RecordedService(root, fixture)
        import secrets
        token = secrets.token_urlsafe(32)
        server = make_server(service, token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (root / 'connection.json').write_text(json.dumps({
            'url': 'http://127.0.0.1:' + str(server.server_port), 'token': token}), encoding='utf-8')
        prompt = ('Use ONLY agent_hub MCP tools. Submit exactly one task with request_id="wait-accept", '
                  'agent="fixture", prompt="WAIT_HUB_PASS", mode="read_only", timeout_seconds=30, cwd='
                  + json.dumps(str(root)) + '. Record returned id. Call wait_for_task with this task_id, '
                  'after_seq=0 and timeout_seconds=0.2. If timed_out=true, call wait_for_task again '
                  'using returned next_seq and timeout_seconds=0.2 until terminal. Do not resubmit. '
                  'Finally call get_result and return the actual answer. Do not use shell or other tools.')
        mcp_config = {'command': sys.executable, 'args': ['-m', 'agent_hub', 'mcp'],
                      'env': {'AGENT_HUB_DATA_DIR': str(root), 'PYTHONUTF8': '1'}}
        tool_names = ('submit_task', 'wait_for_task', 'get_result')
        env = environment(configured[name].env)
        env.update(AGENT_HUB_DATA_DIR=str(root), ENABLE_TOOL_SEARCH='false')
        if name == 'codex':
            argv = [configured[name].executable, 'exec', '--ignore-user-config', '--ignore-rules',
                    '--skip-git-repo-check', '--json', '--sandbox', 'read-only',
                    '-c', 'approval_policy="never"', '-c', 'features.apps=false',
                    '-c', 'windows.sandbox="mxc"',
                    '-c', 'mcp_servers.agent_hub.command=' + json.dumps(sys.executable),
                    '-c', 'mcp_servers.agent_hub.args=["-m","agent_hub","mcp"]',
                    '-c', 'mcp_servers.agent_hub.env.AGENT_HUB_DATA_DIR=' + json.dumps(str(root)),
                    '-c', 'mcp_servers.agent_hub.env.PYTHONUTF8="1"',
                    '-c', 'mcp_servers.agent_hub.required=true',
                    '-c', 'mcp_servers.agent_hub.startup_timeout_sec=60']
            for tool in tool_names:
                argv += ['-c', 'mcp_servers.agent_hub.tools.' + tool + '.approval_mode="approve"',
                         '-c', 'mcp_servers.agent_hub.tools.' + tool + '.enabled=true']
            argv += [prompt]
        else:
            config = root / 'controller-mcp.json'
            config.write_text(json.dumps({'mcpServers': {'agent_hub': mcp_config}}), encoding='utf-8')
            argv = [configured[name].executable, '-p', '--output-format', 'stream-json', '--verbose',
                    '--tools=', '--strict-mcp-config', '--mcp-config=' + str(config),
                    '--setting-sources=user', '--settings', '{"disableAllHooks":true}',
                    '--permission-mode', 'dontAsk',
                    '--allowedTools=' + ','.join('mcp__agent_hub__' + t for t in tool_names), '--', prompt]
        try:
            rc, stdout, stderr, logs = run_process(argv, root, env, threading.Event(),
                time.time() + 180, root / 'controller-logs', lambda *args: None)
            records = service.records
            waits = [r for r in records if r['tool'] == 'wait_for_task']
            passed = (rc == 0 and sum(r['tool'] == 'submit_task' for r in records) == 1
                      and any(r['result']['timed_out'] for r in waits)
                      and any(r['result']['state'] == 'succeeded' and not r['result']['timed_out'] for r in waits)
                      and any(r['tool'] == 'get_result' and r['result'].get('answer') == 'WAIT_HUB_PASS' for r in records))
            result = dict(passed=passed, exit_code=rc, calls=records, logs=logs,
                          executor='deterministic fixture; real controller and MCP')
            (root / 'evidence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            summary[name] = dict(passed=passed, exit_code=rc, tools=[r['tool'] for r in records],
                                 evidence=str(root / 'evidence.json'))
            print(json.dumps({name: summary[name]}, ensure_ascii=False), flush=True)
            if not passed:
                raise RuntimeError(name + ' controller acceptance failed; see private logs')
        finally:
            server.shutdown(); server.server_close(); thread.join(2); service.close()
            (root / 'connection.json').unlink(missing_ok=True)
    print(json.dumps({'passed': True, 'summary': summary}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
