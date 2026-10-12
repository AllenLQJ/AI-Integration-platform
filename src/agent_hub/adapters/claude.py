import json
from pathlib import Path
from .base import CLI
from ..models import HubError


class Claude(CLI):
    capabilities = ['text_only', 'read_only', 'edit_worktree', 'continue']

    def permission_policy(self, mode):
        return {'enforced_by': 'claude_code_tools', 'enforced': True,
                'allows_file_read': mode != 'text_only',
                'allows_write': mode == 'edit_worktree',
                'allows_commands': False, 'os_sandbox': False,
                'workspace': 'git_worktree' if mode == 'edit_worktree' else 'original',
                'scope_note': '工具白名单由 CLI 执行；worktree 不是文件系统沙箱'}

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
        tools = {'text_only': '', 'read_only': 'Read,Glob,Grep',
                 'edit_worktree': 'Read,Glob,Grep,Edit,Write'}[task['mode']]
        args = [self.executable, '-p', '--safe-mode', '--output-format', 'stream-json', '--verbose', '--tools=' + tools,
                '--strict-mcp-config', '--mcp-config=' + str(config),
                '--setting-sources=user', '--settings', '{"disableAllHooks":true}',
                '--permission-mode', 'dontAsk']
        if tools:
            args += ['--allowedTools=' + tools]
        if task.get('resume_session'):
            args += ['--resume', task['resume_session']]
        return args + ['--', task['prompt']]

    def parse(self, output):
        events = []
        for line in output.splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
        result = next((e for e in reversed(events) if isinstance(e, dict) and e.get('type') == 'result'), None)
        if not result or result.get('is_error'):
            raise HubError('CLI_FAILED', 'Claude 没有返回成功的 result')
        return {'answer': result.get('result', ''), 'session_id': result.get('session_id'),
                'models': list(result.get('modelUsage', {}))}
