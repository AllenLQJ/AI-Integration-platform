"""通过已有 SSH 信任关系调用目标机私有 Hub；凭据与模型配置留在目标机。"""
import json
import re
import shlex
import subprocess
from .models import HubError

TOOLS = {'list_agents', 'submit_task', 'get_task', 'get_events', 'wait_for_task',
         'get_result', 'continue_task', 'cancel_task', 'get_work_item', 'claim_controller'}


class SshClient:
    def __init__(self, host, python, data_dir, executable='ssh'):
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,254}', host):
            raise HubError('INVALID_CONFIG', 'SSH host 必须为已有主机别名或 user@host')
        if any(not isinstance(p, str) or not p.startswith('/') or '\x00' in p
               or '\n' in p or '\r' in p for p in (python, data_dir)):
            raise HubError('INVALID_CONFIG', '目标机 Python 与数据目录须为 POSIX 绝对路径')
        self.host, self.python, self.root, self.executable = host, python, data_dir, executable

    def call(self, tool, arguments=None, timeout=15):
        if tool not in TOOLS:
            raise HubError('UNKNOWN_TOOL', '不允许转发此工具')
        if arguments is not None and not isinstance(arguments, dict):
            raise HubError('INVALID_ARGUMENT', 'SSH RPC 参数须为对象')
        payload = json.dumps(arguments or {}, ensure_ascii=False)
        if len(payload.encode()) > 1024 * 1024:
            raise HubError('INVALID_ARGUMENT', 'SSH RPC 参数过大')
        remote = shlex.join([self.python, '-m', 'agent_hub', 'rpc', tool,
                             '--arguments', '-', '--data-dir', self.root])
        argv = [self.executable, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=5', '--', self.host, remote]
        try:
            completed = subprocess.run(argv, input=payload, text=True, capture_output=True,
                                       timeout=max(.1, timeout))
            response = json.loads(completed.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            code = 'DELIVERY_UNKNOWN' if tool in {'submit_task', 'continue_task', 'resume_task'} else 'REMOTE_UNAVAILABLE'
            raise HubError(code, 'SSH 响应未确认；未自动重发，请核对目标机任务') from None
        if not isinstance(response, dict):
            raise HubError('INVALID_RESPONSE', '目标机 Hub 返回格式不正确')
        if 'error' in response:
            error = response['error']
            if (not isinstance(error, dict) or not isinstance(error.get('code'), str)
                    or not isinstance(error.get('message'), str)):
                raise HubError('INVALID_RESPONSE', '目标机 Hub 错误格式不正确')
            raise HubError(error.get('code', 'REMOTE_FAILED'), error.get('message', '目标机调用失败'))
        if completed.returncode:
            raise HubError('REMOTE_UNAVAILABLE', 'SSH 或目标机 Hub 未成功返回')
        return response
