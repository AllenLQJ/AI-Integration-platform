"""Cursor 官方 CLI；执行配置独立于总控的 MCP 配置。"""
import json
import hashlib
import os
from pathlib import Path
from .base import CLI
from ..models import HubError


class Cursor(CLI):
    def __init__(self, executable, env=None, args=None, credentials_file=None, sandbox='enabled'):
        super().__init__(executable, env)
        if args is not None and (not isinstance(args, list) or any(not isinstance(v, str) for v in args)):
            raise HubError('INVALID_CONFIG', 'Cursor args 必须为字符串列表')
        self.args = list(args or [])
        self.credentials_file = credentials_file
        if not isinstance(sandbox, str) or sandbox not in {'enabled', 'disabled'}:
            raise HubError('INVALID_CONFIG', 'Cursor sandbox 必须为 enabled 或 disabled')
        self.sandbox = sandbox

    def version_command(self):
        return [self.executable, *self.args, '--version']

    def permission_policy(self, mode):
        return {'enforced_by': 'cursor_cli_mode_and_permissions', 'enforced': True,
                'allows_file_read': True, 'allows_write': mode == 'edit_worktree',
                'allows_commands': False, 'os_sandbox': False,
                'workspace': 'git_worktree' if mode == 'edit_worktree' else 'original',
                'scope_note': ('ask 模式或文件权限白名单；请求 CLI 沙箱，实际边界需实测；不自动批准工具'
                               if self.sandbox == 'enabled' else
                               '显式权限白名单模式，无 OS 沙箱；禁止 Shell/MCP/WebFetch，不自动批准工具')}

    def command(self, task):
        args = [self.executable, *self.args, '--print', '--output-format', 'stream-json',
                '--sandbox', self.sandbox, '--workspace', task['workspace'], '--trust']
        if task['mode'] == 'read_only':
            args += ['--mode', 'ask']
        elif task['mode'] != 'edit_worktree':
            raise HubError('UNSUPPORTED_MODE', 'Cursor 不支持此模式')
        if task.get('resume_session'):
            args += ['--resume', task['resume_session']]
        return args + ['--', task['prompt']]

    def run(self, task, cancel, event):
        workspace = Path(task['workspace']).resolve()
        # CLI 没有 Claude 的 strict-mcp-config；拒绝加载项目级执行配置。
        for name in ('mcp.json', 'cli.json', 'hooks.json', 'plugins'):
            if (workspace / '.cursor' / name).exists():
                raise HubError('UNSAFE_PROJECT_CONFIG', 'Cursor 执行工作区存在 .cursor/' + name + '，需先人工核对隔离配置')
        # 按实际工作区与模式持久化原生聊天存储，使 resume 能找到前一回合。
        key = hashlib.sha256((task['mode'] + '\0' + str(workspace)).encode()).hexdigest()
        root = Path(task['log_dir']).parent.parent / 'cursor-executor-config' / key
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        allow = ['Read(' + str(workspace) + '/**)']
        deny = ['Shell(*)', 'Mcp(*:*)', 'WebFetch(*)']
        if task['mode'] == 'edit_worktree':
            allow += ['Write(' + str(workspace) + '/**)']
        else:
            deny += ['Write(*)']
        for name, payload in [('cli-config.json', {'version': 1, 'editor': {'vimMode': False},
                              'permissions': {'allow': allow, 'deny': deny}}),
                              ('mcp.json', {'mcpServers': {}})]:
            path = root / name
            temporary = root / (name + '.tmp')
            temporary.touch(mode=0o600)
            temporary.chmod(0o600)
            temporary.write_text(json.dumps(payload), encoding='utf-8')
            os.replace(temporary, path)
        env = dict(self.env, CURSOR_CONFIG_DIR=str(root))
        if self.credentials_file:
            from .grokbot import GrokBot
            # Reuse the officially stored key without copying it into Hub config.
            env['CURSOR_API_KEY'] = GrokBot('cursor-cli-auth', self.credentials_file, env=self.env).key()
        isolated = Cursor(self.executable, env, self.args, sandbox=self.sandbox)
        return CLI.run(isolated, task, cancel, event)

    def parse(self, output):
        try:
            records = [json.loads(line) for line in output.splitlines() if line.strip()]
            result = next((r for r in reversed(records) if isinstance(r, dict) and r.get('type') == 'result'), {})
        except ValueError:
            raise HubError('CLI_FAILED', 'Cursor 没有返回有效 JSON result') from None
        if (not isinstance(result, dict) or result.get('type') != 'result'
                or result.get('subtype') != 'success' or result.get('is_error') is not False
                or not isinstance(result.get('result'), str) or not result.get('session_id')):
            raise HubError('CLI_FAILED', 'Cursor 没有返回成功的 result 与会话 ID')
        return {'answer': result['result'], 'session_id': result['session_id']}
