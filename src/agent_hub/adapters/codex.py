import json
import os
from .base import CLI
from ..models import HubError


class Codex(CLI):
    def __init__(self, executable, env=None, windows_sandbox='mxc'):
        super().__init__(executable, env)
        if windows_sandbox not in {'mxc', 'elevated', 'unelevated'}:
            raise ValueError('unsupported Windows sandbox')
        self.windows_sandbox = windows_sandbox
    def command(self, task):
        args = [self.executable, 'exec']
        if task.get('resume_session'):
            args += ['resume']
        policy = 'read-only' if task['mode'] == 'read_only' else 'workspace-write'
        args += ['--ignore-user-config', '--ignore-rules', '--skip-git-repo-check', '--json',
                 '-c', 'sandbox_mode=' + json.dumps(policy), '-c', 'approval_policy="never"',
                 '-c', 'features.apps=false']
        if os.name == 'nt':
            args += ['-c', 'windows.sandbox=' + json.dumps(self.windows_sandbox)]
        if task.get('resume_session'):
            args += [task['resume_session']]
        return args + [task['prompt']]

    def parse(self, output):
        answer, session, completed = [], None, False
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started': session = event.get('thread_id')
            if event.get('type') == 'turn.completed': completed = True
            if event.get('type') == 'turn.failed':
                raise HubError('CLI_FAILED', 'Codex turn.failed')
            item = event.get('item', {})
            if item.get('type') == 'agent_message': answer.append(item.get('text', ''))
        if not completed:
            raise HubError('INVALID_OUTPUT', 'Codex 没有返回完成事件')
        return {'answer': '\n'.join(answer), 'session_id': session}
