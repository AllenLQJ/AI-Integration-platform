import json
import os
import subprocess
from pathlib import Path
from .adapters.codex import Codex
from .adapters.claude import Claude
from .adapters.muse import Muse
from .adapters.cursor import Cursor
from .adapters.cursor_ide import CursorIDE
from .adapters.grokbot import GrokBot
from .adapters.remote import Remote
from .models import HubError


def scheduler_options(root):
    path = Path(root) / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    limits = config.get('concurrency', {})
    if not isinstance(limits, dict):
        raise HubError('INVALID_CONFIG', 'concurrency 必须为对象')
    global_limit = limits.get('global', 2)
    per_agent = limits.get('per_agent', {})
    if (type(global_limit) is not int or not 1 <= global_limit <= 32 or
            not isinstance(per_agent, dict) or
            any(type(v) is not int or not 1 <= v <= 32 for v in per_agent.values())):
        raise HubError('INVALID_CONFIG', '并发额度必须为 1–32 的整数')
    return {'concurrency': global_limit, 'per_agent_concurrency': per_agent}


def verification_options(root):
    path = Path(root) / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    values = config.get('verification', {})
    ranges = {'max_files': (1000, 1, 1000000), 'max_bytes': (256 * 1024 * 1024, 1, 1024 ** 4),
              'max_entries': (5000, 1, 10000000), 'max_seconds': (10, 1, 3600),
              'join_seconds': (20, 1, 300), 'max_auto_rework': (2, 0, 5)}
    if not isinstance(values, dict) or set(values) - set(ranges):
        raise HubError('INVALID_CONFIG', 'verification 必须为对象，且仅含支持的预算字段')
    result = {}
    for key, (default, minimum, maximum) in ranges.items():
        value = values.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise HubError('INVALID_CONFIG', f'verification.{key} 必须为 {minimum}–{maximum} 的整数')
        result[key] = value
    return result


def data_dir():
    return Path(os.environ.get('AGENT_HUB_DATA_DIR', str(Path.home() / '.agent-hub'))).resolve()


def private_dir(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import win32api, win32con, win32security, ntsecuritycon
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try: sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally: token.Close()
        acl = win32security.ACL()
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
            ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(str(root), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        # Runtime files created before this version may carry explicit broad ACEs.
        files = [root / name for name in ('connection.json', 'config.json', 'daemon.log', 'tasks.db',
                                         'tasks.db-wal', 'tasks.db-shm')]
        for directory in ('logs', 'results'):
            folder = root / directory
            if folder.exists():
                files.append(folder)
                files.extend(folder.rglob('*'))
        for file in files:
            if file.exists():
                import stat
                if getattr(file.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError('runtime data directory must not contain reparse points')
                win32security.SetNamedSecurityInfo(str(file), win32security.SE_FILE_OBJECT,
                    win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                    None, None, acl, None)
    else:
        root.chmod(0o700)


def adapters(root):
    path = Path(root) / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    result = {}
    for name, item in config.get('agents', {}).items():
        if item.get('kind') == 'remote' and item.get('enabled', True):
            result[name] = Remote(item['ssh_host'], item['python'], item['data_dir'],
                                  item['agent'], item['cwd'], item['mode'])
    for name, cls in [('codex', Codex), ('claude', Claude), ('muse', Muse),
                      ('cursor', Cursor), ('cursor_cli', Cursor), ('grokbot', GrokBot)]:
        item = config.get('agents', {}).get(name)
        if item and item.get('enabled', True) and item.get('kind') != 'remote':
            if name == 'cursor' and item.get('kind') == 'ide':
                result[name] = CursorIDE(root, item.get('executable'))
            elif name == 'grokbot':
                result[name] = cls(item['bot_name'], item.get('credentials_file'),
                                   item.get('state_file'), item.get('env'), item.get('session_id'))
            elif name == 'muse': result[name] = cls(item['executable'], item.get('args'), item.get('env'), item.get('session_id'))
            elif name == 'codex': result[name] = cls(item['executable'], item.get('env'), item.get('windows_sandbox', 'mxc'))
            elif name in {'cursor', 'cursor_cli'}:
                result[name] = cls(item['executable'], item.get('env'), item.get('args'), item.get('credentials_file'),
                                   item.get('sandbox', 'enabled'))
            else: result[name] = cls(item['executable'], item.get('env'))
    return result


def remote_node_declaration(name, item, host):
    """Configuration declares identity only; never probes or verifies SSH."""
    import hashlib
    os_platform = item.get('platform')
    if not isinstance(os_platform, str) or os_platform not in {'darwin', 'windows', 'linux'}:
        raise HubError('INVALID_CONFIG', 'remote 节点必须显式声明 platform 为 darwin/windows/linux')
    return (item.get('node_id', 'ssh-' + name), os_platform,
            item.get('host_id', 'ssh-host-' + hashlib.sha256(host.encode()).hexdigest()[:32]))


def artifact_transfer_options(root):
    path = Path(root) / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    values = config.get('artifact_transfer', {})
    ranges = {'max_received_bytes': (1024 ** 3, 1, 1024 ** 4),
              'max_received_files': (10000, 1, 1000000)}
    if not isinstance(values, dict) or set(values) - set(ranges):
        raise HubError('INVALID_CONFIG', 'artifact_transfer 字段不正确')
    result = {}
    for key, (default, low, high) in ranges.items():
        value = values.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise HubError('INVALID_CONFIG', 'artifact_transfer.' + key + ' 必须为范围内整数')
        result[key] = value
    return result
