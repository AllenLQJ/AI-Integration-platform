import json
import os
import subprocess
from pathlib import Path
from .adapters.codex import Codex
from .adapters.claude import Claude
from .adapters.muse import Muse


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
    for name, cls in [('codex', Codex), ('claude', Claude), ('muse', Muse)]:
        item = config.get('agents', {}).get(name)
        if item and item.get('enabled', True):
            if name == 'muse': result[name] = cls(item['executable'], item.get('args'), item.get('env'))
            elif name == 'codex': result[name] = cls(item['executable'], item.get('env'), item.get('windows_sandbox', 'mxc'))
            else: result[name] = cls(item['executable'], item.get('env'))
    return result
