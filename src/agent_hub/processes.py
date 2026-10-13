import os
import signal
import subprocess
import json
import threading
import time
from pathlib import Path
from .models import HubError, redact
from .verification import safe

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


class SpawnError(Exception):
    """Creating the child process itself failed (missing/invalid executable).

    Wraps OSError and, on Windows, pywintypes.error raised by CreateProcess
    (which does not inherit from OSError)."""

    def __init__(self, cause):
        self.cause = cause
        super().__init__(type(cause).__name__ + ': ' + str(cause))


def _spawn_error_types():
    types = [OSError]
    try:
        import pywintypes
        types.append(pywintypes.error)
    except ImportError:
        pass
    return tuple(types)


def spawn_process(argv, cwd, env):
    try:
        if os.name == 'nt':
            from .windows_process import WindowsProcess
            return WindowsProcess(argv, cwd, env)
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except _spawn_error_types() as error:
        raise SpawnError(error) from error


def run_process(argv, cwd, env, cancel, deadline, log_dir, event, allow_hub_events=True):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / 'stdout.log', log_dir / 'stderr.log'
    if not Path(cwd).is_dir():
        raise HubError('INVALID_WORKSPACE', 'workspace directory does not exist: ' + str(cwd),
                       {'workspace': str(cwd)})
    process = spawn_process(argv, cwd, env)
    def pump(stream, path, structured):
        from .store import Store
        tool_names = {}
        session_seen = False
        with path.open('w', encoding='utf-8') as log:
            for line in iter(stream.readline, b''):
                text = line.decode('utf-8', errors='replace')
                try:
                    data = safe(Store.safe(json.loads(text)))
                    if structured and isinstance(data, dict) and data.get('hub_event'):
                        if allow_hub_events:
                            entry = data['hub_event']
                            event(entry['kind'], entry['data'])
                    elif structured and isinstance(data, dict):
                        initialization = data.get('type') == 'thread.started' or (data.get('type') == 'system' and data.get('subtype') == 'init')
                        first_init = initialization and not session_seen
                        if initialization:
                            session_seen = True
                        for kind, payload in stream_events(data):
                            if kind == 'session' and not first_init:
                                continue
                            ident = payload.get('tool_use_id')
                            if kind == 'tool' and ident:
                                if payload.get('name'):
                                    tool_names[ident] = payload['name']
                                elif ident in tool_names:
                                    payload['name'] = tool_names[ident]
                            event(kind, payload)
                    text = json.dumps(log_record(data), ensure_ascii=False) + '\n'
                except ValueError: text = safe(redact(text))
                log.write(text); log.flush()
    readers = [threading.Thread(target=pump, args=(stream, path, stream is process.stdout), daemon=True)
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


def stream_events(data):
    """Map only observed structured records; never retain tool inputs or outputs."""
    kind = data.get('type')
    session_id = data.get('thread_id') if kind == 'thread.started' else data.get('session_id') if kind == 'system' and data.get('subtype') == 'init' else None
    if isinstance(session_id, str) and session_id:
        yield 'session', {'session_id': session_id, 'source': 'cli_init'}
    if kind in {'turn.started', 'turn.completed', 'turn.failed', 'result'}:
        yield 'progress', {'type': kind, 'message': str(data.get('subtype', ''))[:1000]}
    if kind == 'system' and data.get('subtype') == 'init':
        yield 'progress', {'type': 'round.started', 'message': '执行器初始化本轮'}
    denials = data.get('permission_denials')
    for denial in denials if isinstance(denials, list) else []:
        if isinstance(denial, dict):
            yield 'approval', {'name': denial.get('tool_name'), 'status': 'denied'}
    item = data.get('item') or {}
    if kind in {'item.started', 'item.completed'}:
        if item.get('type') == 'agent_message' and kind == 'item.completed':
            yield 'message', {'message': str(item.get('text', ''))[:1000]}
        elif item.get('type') in {'command_execution', 'mcp_tool_call', 'file_change', 'web_search'}:
            status = item.get('status')
            if item.get('exit_code') is not None:
                status = 'success' if item['exit_code'] == 0 else 'failed'
            elif kind == 'item.completed':
                status = 'failed' if status == 'failed' or item.get('error') else 'success'
            yield 'tool', {'name': item.get('tool') or item.get('type'), 'status': status or 'started'}
    message = data.get('message') or {}
    for block in message.get('content', []) if isinstance(message, dict) else []:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text':
            yield 'message', {'message': str(block.get('text', ''))[:1000]}
        elif block.get('type') == 'tool_use':
            yield 'tool', {'name': block.get('name'), 'tool_use_id': block.get('id'), 'status': 'started'}
        elif block.get('type') == 'tool_result':
            yield 'tool', {'tool_use_id': block.get('tool_use_id'), 'status': 'failed' if block.get('is_error') is True else 'success' if block.get('is_error') is False else 'unknown'}
    if kind in {'tool_call', 'tool_result'}:
        call = data.get('tool_call') or {}
        yield 'tool', {'name': data.get('name') or next(iter(call), None),
                       'status': 'failed' if data.get('is_error') else data.get('subtype', 'observed')}


def log_record(data):
    """Keep parseable final replies, but omit complete tool inputs/file contents."""
    if not isinstance(data, dict):
        return data
    data = dict(data)
    if 'permission_denials' in data:
        data['permission_denials'] = [{k: denial[k] for k in ('tool_name', 'tool_use_id') if k in denial}
                                     for denial in data['permission_denials'] if isinstance(denial, dict)] if isinstance(data['permission_denials'], list) else []
    item = data.get('item')
    if isinstance(item, dict) and item.get('type') != 'agent_message':
        data['item'] = {k: v for k, v in item.items() if k in {'id', 'type', 'tool', 'status', 'exit_code'}}
    message = data.get('message')
    if isinstance(message, dict):
        message = dict(message)
        message['content'] = [{k: v for k, v in b.items() if k in {'type', 'name', 'id', 'tool_use_id', 'is_error'}}
                              if isinstance(b, dict) and b.get('type') in {'tool_use', 'tool_result'} else b
                              for b in message.get('content', [])]
        data['message'] = message
    if data.get('type') in {'tool_call', 'tool_result'}:
        data = {k: v for k, v in data.items() if k in {'type', 'subtype', 'name', 'is_error', 'session_id'}}
    return data
