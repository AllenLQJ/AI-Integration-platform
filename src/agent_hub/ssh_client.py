"""通过已有 SSH 信任关系调用目标机私有 Hub；凭据与模型配置留在目标机。"""
import base64
import ntpath
import json
import re
import shlex
import subprocess
import unicodedata
from .models import HubError

TOOLS = {'list_nodes', 'list_agents', 'submit_task', 'get_task', 'get_events', 'wait_for_task',
         'get_result', 'continue_task', 'cancel_task', 'get_work_item', 'claim_controller'}


def validate_remote_path(path, platform):
    """Validate target syntax, independently of the controller's OS."""
    if not isinstance(path, str) or not path or any(unicodedata.category(c) in {'Cc', 'Cs'} for c in path):
        raise HubError('INVALID_CONFIG', '远端路径不得为空或包含控制字符')
    if platform != 'windows':
        valid = path.startswith('/')
    else:
        normalized = path.replace('/', '\\')
        drive, tail = ntpath.splitdrive(normalized)
        valid = bool(re.fullmatch(r'[A-Za-z]:', drive) and tail.startswith('\\'))
        if normalized.startswith('\\\\'):
            parts = normalized[2:].split('\\')
            valid = (len(parts) >= 2 and all(parts[:2]) and
                     all(part not in {'.', '..', '?'} for part in parts[:2]))
        # Win32 device namespaces and invalid filename characters are not paths
        # supported by this transport. Shell metacharacters such as &/$/' are OK.
        valid = valid and not normalized.startswith(('\\\\?\\', '\\\\.\\'))
        valid = valid and not any(c in path for c in '<>"|?*')
        valid = valid and ':' not in normalized[2:]
    if not valid:
        raise HubError('INVALID_CONFIG', '目标机路径须为该平台的绝对路径')
    return path


def remote_settings(platform, shell):
    if not isinstance(platform, str) or platform not in {'posix', 'darwin', 'linux', 'windows'}:
        raise HubError('INVALID_CONFIG', '远端 platform 必须为 posix/darwin/linux/windows')
    shell = shell if shell is not None else ('cmd' if platform == 'windows' else 'posix')
    if not isinstance(shell, str) or shell not in ({'cmd', 'powershell'} if platform == 'windows' else {'posix'}):
        raise HubError('INVALID_CONFIG', '远端 platform 与 shell 不兼容')
    return platform, shell


def powershell_literal(value):
    # CodeGeneration.EscapeSingleQuotedStringContent doubles every character
    # recognized by CharTraits.IsSingleQuote, including the four smart quotes.
    return "'" + ''.join(c * 2 if c in "'\u2018\u2019\u201a\u201b" else c for c in value) + "'"


def powershell_process_script(argv):
    # Avoid NativeCommandProcessor entirely: stderr must never become PowerShell
    # ErrorRecords, and stdout must never pass through a text/object pipeline.
    # UseShellExecute=false with RedirectStandardInput=false inherits stdin.
    # Both output streams drain concurrently to avoid a full-pipe deadlock.
    return (
        "try { $ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
        "$s = New-Object System.Diagnostics.ProcessStartInfo; "
        "$s.FileName = " + powershell_literal(argv[0]) + "; "
        "$s.Arguments = " + powershell_literal(subprocess.list2cmdline(argv[1:])) + "; "
        "$s.UseShellExecute = $false; $s.RedirectStandardInput = $false; "
        "$s.RedirectStandardOutput = $true; $s.RedirectStandardError = $true; "
        "$p = New-Object System.Diagnostics.Process; $p.StartInfo = $s; "
        "if (!$p.Start()) { throw 'Process did not start' }; "
        "$out = $p.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput()); "
        "$err = $p.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError()); "
        "$p.WaitForExit(); [void]$out.GetAwaiter().GetResult(); [void]$err.GetAwaiter().GetResult(); "
        "$code = $p.ExitCode; $p.Dispose(); exit $code "
        "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")


def powershell_command(argv, shell):
    script = powershell_process_script(argv)
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    launcher = ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    # The default PowerShell shell also needs raw output forwarding; invoking
    # powershell.exe as a native command there could merge stderr into stdout.
    command = (powershell_process_script(launcher) if shell == 'powershell'
               else ' '.join(launcher))
    if len(command) > 8000:
        raise HubError('INVALID_CONFIG', 'Windows SSH 命令超过 cmd.exe 安全长度限制')
    return command


class SshClient:
    def __init__(self, host, python, data_dir, executable='ssh', *, platform='posix', shell=None):
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]{0,254}', host):
            raise HubError('INVALID_CONFIG', 'SSH host 必须为已有主机别名或 user@host')
        self.platform, self.shell = remote_settings(platform, shell)
        for path in (python, data_dir):
            validate_remote_path(path, self.platform)
        self.host, self.python, self.root, self.executable = host, python, data_dir, executable

    def call(self, tool, arguments=None, timeout=15):
        if tool not in TOOLS:
            raise HubError('UNKNOWN_TOOL', '不允许转发此工具')
        if arguments is not None and not isinstance(arguments, dict):
            raise HubError('INVALID_ARGUMENT', 'SSH RPC 参数须为对象')
        payload = json.dumps(arguments or {}, ensure_ascii=False)
        if len(payload.encode()) > 1024 * 1024:
            raise HubError('INVALID_ARGUMENT', 'SSH RPC 参数过大')
        delivery = tool in {'submit_task', 'continue_task'}
        command = [self.python, '-X', 'utf8', '-m', 'agent_hub', 'rpc', tool,
                   '--arguments', '-', '--data-dir', self.root]
        remote = (powershell_command(command, self.shell) if self.platform == 'windows'
                  else shlex.join(command))
        argv = [self.executable, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=5', '--', self.host, remote]
        try:
            # SSH transports UTF-8 JSON independently of the Windows host locale.
            # Decode here, rather than in subprocess reader threads, so invalid
            # response bytes follow the same no-retry transport error path.
            completed = subprocess.run(argv, input=payload.encode('utf-8'), capture_output=True,
                                       timeout=max(.1, timeout))
            response = json.loads(completed.stdout.decode('utf-8'))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            code = 'DELIVERY_UNKNOWN' if delivery else 'REMOTE_UNAVAILABLE'
            raise HubError(code, 'SSH 响应未确认；未自动重发，请核对目标机任务') from None
        if not isinstance(response, dict):
            raise HubError('DELIVERY_UNKNOWN' if delivery else 'INVALID_RESPONSE', '目标机 Hub 返回格式不正确')
        # The CLI exits nonzero for RPC errors. A successful get_result RPC may
        # contain a task-level error string (failed/timed_out/cancelled).
        if completed.returncode and 'error' in response:
            error = response['error']
            if (not isinstance(error, dict) or not isinstance(error.get('code'), str)
                    or not isinstance(error.get('message'), str)):
                raise HubError('DELIVERY_UNKNOWN' if delivery else 'INVALID_RESPONSE', '目标机 Hub 错误格式不正确')
            raise HubError(error.get('code', 'REMOTE_FAILED'), error.get('message', '目标机调用失败'))
        if completed.returncode:
            raise HubError('DELIVERY_UNKNOWN' if delivery else 'REMOTE_UNAVAILABLE', 'SSH 或目标机 Hub 未成功返回')
        return response
