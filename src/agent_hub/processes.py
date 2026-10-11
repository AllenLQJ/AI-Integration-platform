import os
import signal
import subprocess
import json
import threading
import time
from pathlib import Path
from .models import HubError, redact

ENV_KEYS = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'USERPROFILE', 'HOME',
            'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'HTTP_PROXY', 'HTTPS_PROXY',
            'ALL_PROXY', 'NO_PROXY', 'LANG', 'LC_ALL', 'PROGRAMDATA', 'ALLUSERSPROFILE',
            'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROCESSOR_ARCHITECTURE',
            'NUMBER_OF_PROCESSORS', 'SYSTEMDRIVE'}


def posix_processes():
    # 独立 session 的 CLI 工具不会收到根进程组的信号，按父子关系追踪。
    output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,lstart='], text=True)
    rows = {}
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3:
            rows[int(fields[0])] = (int(fields[1]), fields[2])
    return rows


def track_descendants(root_pid, tracked):
    rows = posix_processes()
    parents = {root_pid} | {pid for pid, birth in tracked.items()
                             if pid in rows and rows[pid][1] == birth}
    for _ in range(100):
        found = {pid for pid, (parent, _) in rows.items() if parent in parents}
        if found <= parents:
            break
        parents |= found
    for pid in parents - {root_pid}:
        if pid in rows:
            tracked[pid] = rows[pid][1]


def stop_descendants(tracked):
    rows = posix_processes()
    for pid, birth in tracked.items():
        # 已退出或 PID 已复用时不能终止新的无关进程。
        if pid in rows and rows[pid][1] == birth:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def environment(extra=None):
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEYS}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NO_PROXY='127.0.0.1,localhost,::1')
    env.update(extra or {})
    return env


def terminate_tree(process):
    if hasattr(process, 'stop_tree'):
        process.stop_tree()
        return
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(2)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(15)


def run_process(argv, cwd, env, cancel, deadline, log_dir, event):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / 'stdout.log', log_dir / 'stderr.log'
    if os.name == 'nt':
        from .windows_process import WindowsProcess
        process = WindowsProcess(argv, cwd, env)
    else:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    def pump(stream, path):
        from .store import Store
        with path.open('w', encoding='utf-8') as log:
            for line in iter(stream.readline, b''):
                text = line.decode('utf-8', errors='replace')
                try:
                    data = Store.safe(json.loads(text))
                    if isinstance(data, dict) and data.get('hub_event'):
                        entry = data['hub_event']
                        event(entry['kind'], entry['data'])
                    elif isinstance(data, dict) and data.get('type') in {'item.started', 'item.completed', 'turn.failed'}:
                        item = data.get('item', {})
                        event('progress', {'type': data['type'], 'item_type': item.get('type'),
                                           'message': str(item.get('text', ''))[:1000]})
                    text = json.dumps(data, ensure_ascii=False) + '\n'
                except ValueError: text = redact(text)
                log.write(text); log.flush()
    readers = [threading.Thread(target=pump, args=(stream, path), daemon=True)
               for stream, path in ((process.stdout, out_path), (process.stderr, err_path))]
    for reader in readers: reader.start()
    reason = None
    tracked = {}
    try:
        event('process', {'pid': process.pid})
        while process.poll() is None:
            if os.name != 'nt':
                track_descendants(process.pid, tracked)
            if cancel.is_set() or time.time() >= (deadline() if callable(deadline) else deadline):
                reason = 'CANCELLED' if cancel.is_set() else 'TIMED_OUT'
                if os.name != 'nt':
                    stop_descendants(tracked)
                terminate_tree(process)
                break
            time.sleep(.1)
        code = process.wait(15)
    finally:
        if os.name == 'nt': process.close_tree()
        else:
            stop_descendants(tracked)
        for reader in readers: reader.join(15)
        if os.name == 'nt': process.close()
        else:
            process.stdout.close(); process.stderr.close()
    stdout = out_path.read_text(encoding='utf-8', errors='replace')
    stderr = err_path.read_text(encoding='utf-8', errors='replace')
    if reason:
        raise HubError(reason, '本地执行进程树已停止', {'exit_code': code,
                       'artifacts': {'logs': {'stdout': str(out_path), 'stderr': str(err_path)}}})
    return code, stdout, stderr, {'stdout': str(out_path), 'stderr': str(err_path)}
