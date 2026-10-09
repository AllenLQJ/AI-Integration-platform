import json
from pathlib import Path
from .base import CLI
from ..models import HubError


class Claude(CLI):
    def diagnose(self):
        result = super().diagnose()
        result['authentication'] = '现有 CLI 认证；本机已验证 CCR/apiKeyHelper/API 模式'
        result['edit_limit'] = '仅 Read/Glob/Grep/Edit/Write；第一版不开放命令执行'
        return result

    def command(self, task):
        root = Path(task['log_dir'])
        root.mkdir(parents=True, exist_ok=True)
        config = root / 'mcp-empty.json'
        config.write_text('{"mcpServers":{}}', encoding='utf-8')
        tools = '' if task['mode'] == 'read_only' else 'Read,Glob,Grep,Edit,Write'
        args = [self.executable, '-p', '--safe-mode', '--output-format', 'json', '--tools=' + tools,
                '--strict-mcp-config', '--mcp-config=' + str(config),
                '--setting-sources=user', '--settings', '{"disableAllHooks":true}',
                '--permission-mode', 'dontAsk']
        if tools:
            args += ['--allowedTools=Read,Glob,Grep,Edit,Write']
        if task.get('resume_session'):
            args += ['--resume', task['resume_session']]
        return args + ['--', task['prompt']]

    def parse(self, output):
        events = []
        for line in output.splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
        result = next((e for e in reversed(events) if e.get('type') == 'result'), None)
        if not result or result.get('is_error'):
            raise HubError('CLI_FAILED', 'Claude 没有返回成功的 result')
        return {'answer': result.get('result', ''), 'session_id': result.get('session_id')}
