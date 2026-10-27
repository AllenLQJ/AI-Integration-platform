import os
import signal
import subprocess
import json
import logging
import queue
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


LOCAL_NO_PROXY = ('127.0.0.1', 'localhost', '::1')
_ps_warned = False


def posix_processes():
    # 独立 session 的 CLI 工具不会收到根进程组的信号，按父子关系追踪。
    # ps 不可用（受限沙箱、精简容器）时返回 None：仍由进程组信号终止根进程组，
    # 只是无法追踪脱离进程组的后代；不能因此让任务失败。
    global _ps_warned
    try:
        output = subprocess.check_output(['ps', '-axo', 'pid=,ppid=,lstart='], text=True,
                                         stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        if not _ps_warned:
            _ps_warned = True
            logging.getLogger(__name__).warning(
                'ps 不可用（%s），无法追踪脱离进程组的子进程', type(error).__name__)
        return None
    rows = {}
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3:
            rows[int(fields[0])] = (int(fields[1]), fields[2])
    return rows


def track_descendants(root_pid, tracked):
    rows = posix_processes()
    if rows is None:
        return
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


def stop_descendants(tracked, root_group=None):
    rows = posix_processes()
    if rows is None:
        # 无法确认启动时间时不按 PID 发信号，避免误杀复用了 PID 的无关进程；
        # 根进程组仍有成员时其 ID 不会被复用，可安全回收留在组内的子进程。
        if root_group is not None:
            try:
                os.killpg(root_group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return
    for pid, birth in tracked.items():
        # 已退出或 PID 已复用时不能终止新的无关进程。
        if pid in rows and rows[pid][1] == birth:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def environment(extra=None):
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEYS}
    # 合并用户已有的排除项；小写 no_proxy 在 urllib/httpx 中优先，必须一并覆盖。
    bypass = []
    for key in [k for k in env if k.upper() == 'NO_PROXY']:
        bypass.extend(item.strip() for item in env.pop(key).split(','))
    bypass = list(dict.fromkeys(item for item in (*LOCAL_NO_PROXY, *bypass) if item))
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NO_PROXY=','.join(bypass))
    if os.name != 'nt':
        # Windows 环境变量不区分大小写，重复键会互相覆盖。
        env['no_proxy'] = env['NO_PROXY']
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


MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024


def run_process(argv, cwd, env, cancel, deadline, log_dir, event, allow_hub_events=True,
                parse_cli_events=True, stdout_line=None, capture_stdout=True):
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / 'stdout.log', log_dir / 'stderr.log'
    if not Path(cwd).is_dir():
        raise HubError('INVALID_WORKSPACE', 'workspace directory does not exist: ' + str(cwd),
                       {'workspace': str(cwd)})
    process = spawn_process(argv, cwd, env)
    failures = queue.Queue(maxsize=2)
    def pump(stream, path, structured):
        from .store import Store
        tool_names = {}
        session_seen = False
        with path.open('w', encoding='utf-8') as log:
            for line in iter(lambda: stream.readline(MAX_LINE_BYTES + 1), b''):
                if len(line) > MAX_LINE_BYTES:
                    log.write('[omitted oversized output line]\n'); log.flush()
                    raise HubError('OUTPUT_TOO_LARGE', '执行输出超过单行字节预算', {'limit_bytes': MAX_LINE_BYTES})
                text = line.decode('utf-8', errors='replace')
                if structured and stdout_line is not None:
                    stdout_line(text)
                if not parse_cli_events:
                    # Protocol consumers receive original lines in memory, separately
                    # from sanitized logs. Never parse arbitrary tool output as events.
                    if len(text) > 8192:
                        logged = '[omitted oversized output line]\n'
                    else:
                        logged = redact(text.replace('\r\n', '\n'))
                        if '://' in logged:
                            logged = safe(logged)
                    log.write(logged); log.flush()
                    continue
                try:
                    data = json.loads(text)
                except ValueError:
                    text = safe(redact(text))
                else:
                    data = safe(Store.safe(data))
                    if structured and isinstance(data, dict) and data.get('hub_event'):
                        if allow_hub_events:
                            entry = data['hub_event']
                            event(entry['kind'], entry['data'])
                    elif structured and parse_cli_events and isinstance(data, dict):
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
                    text = json.dumps(log_record(data) if parse_cli_events else data, ensure_ascii=False) + '\n'
                log.write(text); log.flush()

    def read_stream(stream, path, structured):
        try:
            pump(stream, path, structured)
        except Exception as error:
            limited = isinstance(error, HubError) and error.code == 'OUTPUT_TOO_LARGE'
            failures.put((path.stem, type(error).__name__, limited,
                          error.details.get('limit_bytes') if limited else None))

    readers = [threading.Thread(target=read_stream, args=(stream, path, stream is process.stdout), daemon=True)
               for stream, path in ((process.stdout, out_path), (process.stderr, err_path))]
    for reader in readers: reader.start()
    reason = None
    code = None
    wait_timed_out = False
    tracked = {}

    def stop_process():
        nonlocal wait_timed_out
        try:
            terminate_tree(process)
        except subprocess.TimeoutExpired:
            wait_timed_out = True
    try:
        event('process', {'pid': process.pid})
        while process.poll() is None:
            if os.name != 'nt':
                track_descendants(process.pid, tracked)
            if cancel.is_set() or time.time() >= (deadline() if callable(deadline) else deadline):
                reason = 'CANCELLED' if cancel.is_set() else 'TIMED_OUT'
                if os.name != 'nt':
                    stop_descendants(tracked)
                stop_process()
                break
            if not failures.empty():
                stop_process()
                break
            time.sleep(.1)
        try:
            code = process.poll() if wait_timed_out else process.wait(15)
        except subprocess.TimeoutExpired:
            wait_timed_out = True
            stop_process()
            code = process.poll()
    except BaseException:
        if os.name != 'nt':
            try:
                track_descendants(process.pid, tracked)
            except OSError:
                pass
        stop_process()
        raise
    finally:
        if os.name == 'nt': process.close_tree()
        else:
            stop_descendants(tracked, process.pid)
        for reader in readers: reader.join(15)
        if os.name == 'nt': process.close()
        else:
            process.stdout.close(); process.stderr.close()
    logs = {'stdout': str(out_path), 'stderr': str(err_path)}

    def fail(error_code, message, **details):
        # Re-evaluate at publication as cancellation/deadline can change while
        # waiting for the process or draining its readers.
        if cancel.is_set() or reason == 'CANCELLED':
            error_code = 'CANCELLED'
        elif reason == 'TIMED_OUT' or time.time() >= (deadline() if callable(deadline) else deadline):
            error_code = 'TIMED_OUT'
        evidence = dict(exit_code=code, local_process_stopped=code is not None, artifacts={'logs': logs})
        if wait_timed_out:
            evidence['wait_timeout'] = True
        evidence.update(details)
        raise HubError(error_code, message, evidence)

    if reason:
        fail(reason, '本地执行进程树已停止' if code is not None else '已请求停止，本地进程停止尚未确认')
    if not failures.empty():
        stream, cause, limited, limit = failures.get_nowait()
        if limited:
            logs[stream + '_truncated'] = True
        fail('OUTPUT_TOO_LARGE' if limited else 'PROCESS_STREAM_ERROR',
             '执行输出超过单行预算' if limited else '执行输出采集失败，请检查任务日志',
             stream=stream, cause=cause, limit_bytes=limit)
    if wait_timed_out:
        fail('PROCESS_STREAM_ERROR', '执行进程退出未确认，请核对任务日志', cause='ProcessWaitTimeout')
    if any(reader.is_alive() for reader in readers):
        fail('PROCESS_STREAM_ERROR', '执行输出采集未能及时结束，请核对任务日志', cause='ReaderJoinTimeout')
    stdout = ''
    if capture_stdout:
        with out_path.open('rb') as output:
            content = output.read(MAX_STDOUT_BYTES + 1)
        if len(content) > MAX_STDOUT_BYTES:
            logs['stdout_truncated'] = True
            fail('OUTPUT_TOO_LARGE', '执行输出超过内存预算，完整已采集日志保留在磁盘', limit_bytes=MAX_STDOUT_BYTES)
        stdout = content.decode('utf-8', errors='replace')
    with err_path.open('rb') as errors:
        size = errors.seek(0, os.SEEK_END)
        errors.seek(max(0, size - MAX_STDERR_BYTES))
        stderr = errors.read(MAX_STDERR_BYTES).decode('utf-8', errors='replace')
    if size > MAX_STDERR_BYTES:
        logs['stderr_truncated'] = True
    if cancel.is_set() or time.time() >= (deadline() if callable(deadline) else deadline):
        fail('TIMED_OUT', '执行收尾期间收到停止请求或达到期限')
    return code, stdout, stderr, logs


OUTPUT_EXCERPT_BUDGET = 16384
READ_TOOL_NAMES = frozenset({'Read', 'read_file', 'ReadFile', 'read'})
WRITE_TOOL_NAMES = frozenset({'Write', 'Edit', 'write_file', 'edit_file', 'ApplyPatch', 'apply_patch', 'NotebookEdit'})
COMMAND_TOOL_NAMES = frozenset({'Bash', 'Shell', 'bash', 'shell', 'command_execution'})
# Cursor structured tool_call keys observed in CLI streams — never substring-guess "read".
CURSOR_READ_KEYS = frozenset({'readToolCall', 'read_file', 'readFileToolCall'})
CURSOR_WRITE_KEYS = frozenset({'editToolCall', 'writeToolCall', 'edit_file', 'write_file', 'applyPatchToolCall'})
CURSOR_COMMAND_KEYS = frozenset({'shellToolCall', 'shell', 'commandToolCall'})


def _clip(value, limit=1000):
    text = redact(str(value if value is not None else ''))
    return text[:limit]


def _safe_scalar(value, limit=500):
    if not isinstance(value, str) or not value or '\x00' in value:
        return None
    text = redact(value)
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode('utf-8', errors='ignore')


def _bound_excerpts(pairs, budget=OUTPUT_EXCERPT_BUDGET):
    """Redact then share a UTF-8 byte budget across stdout/stderr excerpts."""
    result, truncated = {}, []
    remaining = budget
    for key, value in pairs:
        if not isinstance(value, str) or not value:
            continue
        text = redact(value)
        encoded = text.encode('utf-8')
        if len(encoded) <= remaining:
            result[key] = text
            remaining -= len(encoded)
        else:
            result[key] = encoded[:remaining].decode('utf-8', errors='ignore')
            truncated.append(key)
            remaining = 0
        if remaining <= 0:
            break
    return result, truncated


def _explicit_test_summary(item):
    """Only accept structured test aggregates; never infer from command substrings."""
    for key in ('test_summary', 'summary'):
        summary = item.get(key)
        if isinstance(summary, str) and summary.strip():
            return _safe_scalar(summary, 2000)
    counts = item.get('tests') or item.get('test_results')
    if isinstance(counts, dict):
        parts = []
        for key in ('passed', 'failed', 'skipped', 'total', 'errors'):
            if type(counts.get(key)) is int:
                parts.append(f'{key}={counts[key]}')
        if parts:
            return ', '.join(parts)
    return None


def _path_fields(path):
    if not isinstance(path, str) or not path:
        return {}
    safe = _safe_scalar(path, 1000)
    if not safe:
        return {}
    return {'path': safe, 'path_display': _safe_scalar(Path(safe).name or safe, 500)}


def _read_range_fields(item):
    start = item.get('offset') if type(item.get('offset')) is int else item.get('start')
    limit = item.get('limit') if type(item.get('limit')) is int else None
    end = item.get('end') if type(item.get('end')) is int else (
        start + limit if type(start) is int and type(limit) is int else None)
    read_range = {}
    if type(start) is int and 0 <= start <= 10**9:
        read_range['start'] = start
    if type(end) is int and 0 <= end <= 10**9:
        read_range['end'] = end
    return {'read_range': read_range} if read_range else {}


def _classify_tool_name(name):
    if name in READ_TOOL_NAMES:
        return 'file_read'
    if name in WRITE_TOOL_NAMES:
        return 'file_change'
    if name in COMMAND_TOOL_NAMES:
        return 'command'
    return 'system'


def _operation_from_item(item, phase):
    """Extract whitelist operation fields from Codex/Cursor structured items."""
    if not isinstance(item, dict):
        return None, {}
    item_type = item.get('type')
    operation_id = item.get('id') if isinstance(item.get('id'), str) else None
    tool_name = item.get('tool') or item_type
    status = item.get('status')
    if item.get('exit_code') is not None:
        status = 'completed' if item['exit_code'] == 0 else 'failed'
    elif phase == 'completed':
        status = 'failed' if status == 'failed' or item.get('error') else 'completed'
    else:
        status = 'started'
    operation = {}
    if tool_name:
        name = _safe_scalar(str(tool_name), 200)
        if name:
            operation['tool_name'] = name
    # Explicit type / tool whitelist first — never "has path ⇒ file_change".
    if item_type in {'read', 'file_read'} or tool_name in READ_TOOL_NAMES:
        kind = 'file_read'
        operation.update(_path_fields(item.get('path') or item.get('filename') or item.get('file_path')))
        operation.update(_read_range_fields(item))
    elif item_type in {'file_change', 'file_edit'} or tool_name in WRITE_TOOL_NAMES:
        kind = 'file_change'
        operation.update(_path_fields(item.get('path') or item.get('filename') or item.get('file_path')))
        if isinstance(item.get('changes'), str):
            summary = _safe_scalar(item['changes'], 2000)
            if summary:
                operation['change_summary'] = summary
    elif item_type == 'command_execution' or tool_name in COMMAND_TOOL_NAMES or (
            item_type != 'mcp_tool_call' and item.get('command') and item_type not in {
                'read', 'file_read', 'file_change', 'file_edit', 'web_search'}):
        kind = 'command'
        command = item.get('command') or item.get('command_display') or item.get('args')
        if isinstance(command, list):
            command = ' '.join(str(part) for part in command[:40])
        display = _safe_scalar(command, 4000) if command is not None else None
        if display:
            operation['command_display'] = display
        if type(item.get('exit_code')) is int:
            operation['exit_code'] = item['exit_code']
        cwd = _safe_scalar(item.get('cwd') or item.get('working_directory'), 500)
        if cwd:
            operation['cwd_display'] = Path(cwd).name or cwd
        excerpts, truncated = _bound_excerpts([
            ('stdout_excerpt', item.get('aggregated_output') or item.get('stdout')),
            ('stderr_excerpt', item.get('stderr')),
        ])
        operation.update(excerpts)
        if truncated:
            operation['truncated_fields'] = truncated
        test_summary = _explicit_test_summary(item)
        if test_summary and item.get('is_test') is True:
            kind = 'test'
            operation['test_summary'] = test_summary
    elif item_type == 'mcp_tool_call':
        kind = _classify_tool_name(tool_name)
        if kind == 'file_read':
            operation.update(_path_fields(item.get('path') or item.get('filename') or item.get('file_path')))
            operation.update(_read_range_fields(item))
        elif kind == 'file_change':
            operation.update(_path_fields(item.get('path') or item.get('filename') or item.get('file_path')))
        elif kind == 'command':
            command = item.get('command') or item.get('command_display')
            display = _safe_scalar(command, 4000) if command is not None else None
            if display:
                operation['command_display'] = display
        else:
            kind = 'system'
            # Unknown MCP tool: metadata only, mark coverage gap.
            operation['truncated_fields'] = list(operation.get('truncated_fields') or []) + ['mcp_tool_details']
    elif item_type == 'web_search':
        kind = 'system'
    else:
        kind = 'system'
    activity = {
        'kind': kind,
        'operation_id': operation_id,
        'phase': 'started' if phase == 'started' else ('failed' if status == 'failed' else 'completed'),
        'status': 'started' if status == 'started' else ('failed' if status == 'failed' else 'completed'),
        'provenance': 'executor_observed',
        'source': 'cli_structured',
        'completeness': 'unavailable' if kind == 'system' and item_type == 'mcp_tool_call' else 'recorded',
        'operation': operation or None,
        'title': _safe_scalar(str(tool_name or item_type or 'operation'), 200),
    }
    activity = {k: v for k, v in activity.items() if v is not None}
    # Legacy tool payload stays minimal; never attach raw result bodies.
    tool_payload = {
        'name': tool_name,
        'status': 'success' if status == 'completed' else status,
        'tool_use_id': operation_id,
    }
    if operation:
        tool_payload['operation'] = operation
        tool_payload['activity_kind'] = kind
    if activity.get('completeness') == 'unavailable':
        tool_payload['completeness'] = 'unavailable'
    tool_payload = {k: v for k, v in tool_payload.items() if v is not None}
    return activity, tool_payload


def _claude_activity_from_tool_use(block):
    name = block.get('name')
    ident = block.get('id') if isinstance(block.get('id'), str) else None
    kind = _classify_tool_name(name)
    operation = {'tool_name': _safe_scalar(str(name), 200)} if name else {}
    raw_input = block.get('input') if isinstance(block.get('input'), dict) else {}
    path = raw_input.get('path') or raw_input.get('file_path') or raw_input.get('filename')
    if isinstance(path, str) and kind in {'file_read', 'file_change'}:
        operation.update(_path_fields(path))
    if kind == 'file_read':
        operation.update(_read_range_fields(raw_input))
    command = raw_input.get('command')
    if isinstance(command, str) and kind == 'command':
        display = _safe_scalar(command, 4000)
        if display:
            operation['command_display'] = display
    # Glob and other tools stay system — do not guess file_read from path alone.
    return {
        'kind': kind,
        'operation_id': ident,
        'phase': 'started',
        'status': 'started',
        'provenance': 'executor_observed',
        'source': 'cli_structured',
        'title': _safe_scalar(str(name or 'tool'), 200),
        'operation': operation or None,
        'completeness': 'recorded' if kind != 'system' else 'unavailable',
    }


def _cursor_activity_from_tool_call(data, kind_name):
    call = data.get('tool_call') or {}
    name = data.get('name') or (next(iter(call), None) if isinstance(call, dict) else None)
    status = 'failed' if data.get('is_error') is True else (
        'completed' if kind_name == 'tool_result' and data.get('is_error') is False else (
            data.get('subtype') if isinstance(data.get('subtype'), str) else (
                'started' if kind_name == 'tool_call' else 'unknown')))
    if status not in {'started', 'completed', 'failed', 'unknown', 'success', 'observed'}:
        status = 'unknown'
    if status == 'success':
        status = 'completed'
    if status == 'observed':
        status = 'started' if kind_name == 'tool_call' else 'unknown'
    ident = data.get('call_id') or data.get('tool_call_id')
    if not isinstance(ident, str):
        ident = None
    activity_kind = 'system'
    operation = {'tool_name': _safe_scalar(str(name), 200)} if name else {}
    if isinstance(call, dict):
        for key, detail in call.items():
            detail = detail if isinstance(detail, dict) else {}
            path = detail.get('path') or detail.get('file_path')
            command = detail.get('command')
            if key in CURSOR_READ_KEYS:
                activity_kind = 'file_read'
                operation.update(_path_fields(path))
                operation.update(_read_range_fields(detail))
            elif key in CURSOR_WRITE_KEYS:
                activity_kind = 'file_change'
                operation.update(_path_fields(path))
            elif key in CURSOR_COMMAND_KEYS:
                activity_kind = 'command'
                if isinstance(command, str):
                    display = _safe_scalar(command, 4000)
                    if display:
                        operation['command_display'] = display
            # Unknown keys: leave system; never substring-match "read".
    mapped_status = 'failed' if status == 'failed' else (
        'started' if status == 'started' else 'completed' if status == 'completed' else 'unknown')
    return {
        'tool': {'name': name, 'status': mapped_status if mapped_status != 'completed' else 'success',
                 'tool_use_id': ident},
        'activity': {
            'kind': activity_kind,
            'operation_id': ident,
            'phase': mapped_status,
            'status': mapped_status,
            'provenance': 'executor_observed',
            'source': 'cli_structured',
            'title': _safe_scalar(str(name), 200) if name else None,
            'operation': operation or None,
            'completeness': 'recorded' if activity_kind != 'system' else 'unavailable',
        },
    }


def stream_events(data):
    """Map observed structured records; extract safe fields before log redaction."""
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
            role = item.get('role') if item.get('role') in {'user', 'assistant', 'system', 'tool'} else 'assistant'
            payload = {'message': _clip(item.get('text', ''), 1000), 'role': role}
            if isinstance(item.get('id'), str):
                payload['provider_message_id'] = item['id'][:200]
            if type(item.get('revision')) is int:
                payload['provider_revision'] = item['revision']
            yield 'message', payload
        elif item.get('type') in {'command_execution', 'mcp_tool_call', 'file_change', 'web_search',
                                  'file_edit', 'read', 'file_read'} or item.get('command'):
            phase = 'started' if kind == 'item.started' else 'completed'
            activity, tool_payload = _operation_from_item(item, phase)
            if tool_payload:
                # Tool events stay metadata-only (name/status/id/safe operation); never raw results.
                safe_tool = {k: tool_payload[k] for k in (
                    'name', 'status', 'tool_use_id', 'operation', 'activity_kind', 'completeness')
                    if k in tool_payload and tool_payload[k] is not None}
                yield 'tool', safe_tool
            if activity:
                yield 'activity', activity
        else:
            yield 'tool', {'name': item.get('tool') or item.get('type') or 'unknown',
                           'status': 'started' if kind == 'item.started' else 'unknown',
                           'completeness': 'unavailable'}
    message = data.get('message') or {}
    # Preserve upstream role: Cursor/Claude user prompt echoes must not become assistant.
    stream_role = 'user' if kind in {'user', 'human'} else (
        'assistant' if kind in {'assistant', 'ai'} else (
            data.get('role') if data.get('role') in {'user', 'assistant', 'system', 'tool'} else None))
    for block in message.get('content', []) if isinstance(message, dict) else []:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text':
            role = stream_role or 'assistant'
            payload = {'message': _clip(block.get('text', ''), 1000), 'role': role}
            mid = message.get('id') if isinstance(message.get('id'), str) else data.get('uuid')
            if isinstance(mid, str):
                payload['provider_message_id'] = mid[:200]
            if type(data.get('provider_revision')) is int:
                payload['provider_revision'] = data['provider_revision']
            elif type(message.get('revision')) is int:
                payload['provider_revision'] = message['revision']
            yield 'message', payload
        elif block.get('type') == 'tool_use':
            name = block.get('name')
            ident = block.get('id') if isinstance(block.get('id'), str) else None
            yield 'tool', {'name': name, 'tool_use_id': ident, 'status': 'started'}
            activity = _claude_activity_from_tool_use(block)
            yield 'activity', {k: v for k, v in activity.items() if v is not None}
        elif block.get('type') == 'tool_result':
            ident = block.get('tool_use_id') if isinstance(block.get('tool_use_id'), str) else None
            status = 'failed' if block.get('is_error') is True else 'success' if block.get('is_error') is False else 'unknown'
            yield 'tool', {'tool_use_id': ident, 'status': status}
            # Compatibility: unknown outcome stays unknown; activity is additive.
            yield 'activity', {
                'kind': 'system',
                'operation_id': ident,
                'phase': 'failed' if status == 'failed' else 'completed' if status == 'success' else 'unknown',
                'status': 'failed' if status == 'failed' else 'completed' if status == 'success' else 'unknown',
                'provenance': 'executor_observed',
                'source': 'cli_structured',
                'completeness': 'recorded',
            }
    if kind in {'tool_call', 'tool_result'}:
        mapped = _cursor_activity_from_tool_call(data, kind)
        tool = {k: v for k, v in mapped['tool'].items() if v is not None}
        yield 'tool', tool
        yield 'activity', {k: v for k, v in mapped['activity'].items() if v is not None}


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
