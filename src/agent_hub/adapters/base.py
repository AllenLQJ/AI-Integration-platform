import os
import subprocess
from pathlib import Path
from ..models import HubError
from ..processes import environment, run_process


class CLI:
    remote_cancel = True
    capabilities = ['read_only', 'edit_worktree', 'continue']

    def __init__(self, executable, env=None):
        self.executable = str(executable)
        self.env = env or {}

    def permission_policy(self, mode):
        return {'enforced_by': 'unknown', 'enforced': False, 'allows_write': None}

    def diagnose(self):
        exists = Path(self.executable).is_file()
        version = None
        if exists:
            try:
                version = subprocess.run([self.executable, '--version'], env=environment(self.env),
                                         capture_output=True, text=True, timeout=10).stdout.strip()[:100]
            except (OSError, subprocess.TimeoutExpired):
                pass
        return {'available': exists, 'version': version, 'platform': os.name,
                'authentication': '使用 CLI 现有认证；有效性由实际任务确认',
                'capabilities': self.capabilities,
                'permissions': {mode: self.permission_policy(mode) for mode in self.capabilities if mode != 'continue'}}

    def run(self, task, cancel, event):
        if not Path(self.executable).is_file():
            raise HubError('AGENT_UNAVAILABLE', '执行器路径不存在')
        session_id = None
        def observed(kind, data):
            nonlocal session_id
            if kind == 'session':
                if data.get('source') != 'cli_init' or session_id is not None:
                    return
                session_id = data['session_id']
            event(kind, data)
        rc, out, err, logs = run_process(self.command(task), task['workspace'], environment(self.env),
                                       cancel, task['deadline'], task['log_dir'], observed, allow_hub_events=False)
        if rc:
            raise HubError('CLI_FAILED', '执行器退出码 ' + str(rc) + ': ' + err[-1000:],
                           {'exit_code': rc, 'artifacts': {'logs': logs}})
        try: result = self.parse(out)
        except HubError as error:
            error.details.update(exit_code=rc, artifacts={'logs': logs})
            raise
        # Final result records cannot replace the stdout initialization session.
        result['session_id'] = session_id
        if 'blocked by policy' in err or 'Failed to write' in err or 'Failed to delete' in err:
            result['warnings'] = ['执行日志包含工具失败，请检查差异和日志；CLI 回合结束不等于任务目标已完成']
        result.update(exit_code=rc, artifacts={'logs': logs})
        return result
