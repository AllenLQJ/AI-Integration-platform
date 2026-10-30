# AI-Integration-platform 全量问题优化与重构方案

本文件提供针对 `AllenLQJ/AI-Integration-platform` 仓库 5 大核心模块的完整优化实现。你可以直接将对应代码块覆盖到仓库的同名文件中，或按模块逐步应用。

## 1. 依赖管理与打包配置优化 (`pyproject.toml`)

### 优化点说明
- **解除死锁版本约束**：将 `mcp==1.30.0` 与 `httpx==0.28.1` 放宽为语义化兼容区间（`>=1.30.0,<2.0.0` 和 `>=0.28.1,<1.0.0`），避免与其他本地 MCP 组件冲突，将精确锁版本交由 `uv.lock` 管理。
- **补充开发与测试依赖组 (`[project.optional-dependencies]`)**：方便 CI 与本地执行 `uv pip install -e ".[dev]"`。

### 完整替换代码 (`pyproject.toml`)

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "local-agent-hub"
version = "0.3.1"
description = "Local persistent task hub with portable executor plugins and command bridges"
requires-python = ">=3.10"
dependencies = [
    "mcp>=1.30.0,<2.0.0",
    "httpx>=0.28.1,<1.0.0",
    "Pillow>=11,<13",
    "pywin32>=312; platform_system=='Windows'",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
]

[project.scripts]
agent-hub = "agent_hub.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
agent_hub = [
    "dashboard_static/*.html",
    "dashboard_static/*.css",
    "dashboard_static/*.mjs",
    "skills/*/SKILL.md",
]
```

## 2. SQLite 并发写锁与连接优化 (`src/agent_hub/store.py`)

### 优化点说明
- **连接级 PRAGMA 持久化生效**：SQLite 中 `PRAGMA journal_mode=WAL;` 是库文件级属性，但 `PRAGMA synchronous=NORMAL;`、`PRAGMA busy_timeout=5000;` 和 `PRAGMA foreign_keys=ON;` 属于**连接级属性**。原实现每次调用 `with store.connection() as db:` 都会新建连接但未设置连接级 `busy_timeout` 与 `synchronous`。
- **读写分离锁保护 (`write_connection`)**：在 `ThreadingHTTPServer` 多线程并发写入 `events` 和 `tasks` 表时，增加进程内可重入写锁 `threading.RLock()`，从根源消除 `sqlite3.OperationalError: database is locked`。

### 核心重构代码 (`src/agent_hub/store.py`)

```python
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = int(os.environ.get("AGENT_HUB_SQLITE_BUSY_TIMEOUT_MS", "5000"))


class Store:
    """SQLite 数据库存储管理类（支持多线程 WAL 并发读与序列化写）。"""

    def __init__(self, path):
        self.path = str(path)
        self._write_lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection(write=True) as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    digest TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    workspace_quarantined INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, timestamp REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS events_task ON events(task_id, seq);
                CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
            ''')

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")

    @contextmanager
    def connection(self, write: bool = False):
        """建立 SQLite 数据库连接上下文句柄，自动配置连接级 PRAGMA 与写锁协调。"""
        timeout_sec = max(30.0, DEFAULT_BUSY_TIMEOUT_MS / 1000.0)
        if write:
            with self._write_lock:
                conn = sqlite3.connect(self.path, timeout=timeout_sec, check_same_thread=False)
                self._configure_connection(conn)
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        else:
            conn = sqlite3.connect(self.path, timeout=timeout_sec, check_same_thread=False)
            self._configure_connection(conn)
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
```

## 3. 跨平台子进程树隔离与孤儿进程清理 (`src/agent_hub/processes.py` & `src/agent_hub/windows_process.py`)

### 优化点说明
- **POSIX 独立进程组启动 (`start_new_session=True`)**：原 `ProcessRunner.start()` 与 `run_command()` 直接调用 `subprocess.Popen` 而未开启 `start_new_session=True`，导致 `terminate_process_tree(pid)` 调用 `os.getpgid(pid)` 时可能获取到守护进程自身的进程组 ID，误杀守护进程或漏杀孙进程！开启 `start_new_session=True` 后，子进程本身即为新进程组 Leader（`pgid == pid`），配合 `stop_descendants` 先清理脱离会话的后代，再 `os.killpg` 兜底。
- **Windows Job Object 强绑定 (`src/agent_hub/windows_process.py`)**：利用项目已声明的 `pywin32` 依赖，创建带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 标志的 Windows Job Object，保证即使守护进程被强制杀死，所有派生的 CLI / 浏览器子进程也会由操作系统内核自动回收。

### 核心重构代码 (`src/agent_hub/processes.py`)

```python
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

ENV_KEYS = {
    'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'USERPROFILE', 'HOME',
    'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'HTTP_PROXY', 'HTTPS_PROXY',
    'ALL_PROXY', 'NO_PROXY', 'LANG', 'LC_ALL', 'PROGRAMDATA', 'ALLUSERSPROFILE',
    'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'PROCESSOR_ARCHITECTURE',
    'NUMBER_OF_PROCESSORS', 'SYSTEMDRIVE'
}

LOCAL_NO_PROXY = ('127.0.0.1', 'localhost', '::1')
_ps_warned = False


def posix_processes():
    global _ps_warned
    try:
        output = subprocess.check_output(
            ['ps', '-axo', 'pid=,ppid=,lstart='],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if not _ps_warned:
            _ps_warned = True
            logging.getLogger(__name__).warning(
                'ps 不可用 (%s)，仅通过进程组信号回收子进程', type(error).__name__
            )
        return None
    rows = {}
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3:
            try:
                rows[int(fields[0])] = (int(fields[1]), fields[2])
            except ValueError:
                continue
    return rows


def track_descendants(root_pid, tracked):
    rows = posix_processes()
    if rows is None:
        return
    parents = {root_pid} | {
        pid for pid, birth in tracked.items()
        if pid in rows and rows[pid][1] == birth
    }
    for _ in range(100):
        found = {pid for pid, (parent, _) in rows.items() if parent in parents}
        if found <= parents:
            break
        parents |= found
    for pid in parents - {root_pid}:
        if pid in rows:
            tracked[pid] = rows[pid][1]


def stop_descendants(tracked, sig=signal.SIGTERM):
    rows = posix_processes()
    if rows is None:
        return
    for pid, birth in list(tracked.items()):
        if pid in rows and rows[pid][1] == birth:
            try:
                os.kill(pid, sig)
            except OSError:
                pass


def _popen_session_kwargs():
    """确保子进程位于独立进程组/会话中，防止 os.killpg 误伤守护进程自身。"""
    if os.name == 'nt':
        creationflags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        return {'creationflags': creationflags}
    return {'start_new_session': True}


def terminate_process_tree(pid, tracked=None, graceful_timeout=5.0):
    """优雅终止并在超时后强制杀死指定的 PID 及其完整后代进程树。"""
    if pid is None:
        return
    if os.name == 'nt':
        from .windows_process import terminate_win32_process_tree
        terminate_win32_process_tree(pid)
        return

    daemon_pgid = os.getpgrp()
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None

    # 1. 发送 SIGTERM 给进程组及已追踪的脱离会话后代
    if pgid is not None and pgid != daemon_pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    if tracked:
        stop_descendants(tracked, signal.SIGTERM)

    # 2. 等待进程退出，避免无条件死等
    deadline = time.monotonic() + max(0.1, min(graceful_timeout, 5.0))
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.05)

    # 3. 强制 SIGKILL 兜底
    if pgid is not None and pgid != daemon_pgid:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    if tracked:
        stop_descendants(tracked, signal.SIGKILL)


def clean_env(extra_env=None):
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEYS}
    no_proxy = set(filter(None, env.get('NO_PROXY', '').split(',')))
    no_proxy.update(LOCAL_NO_PROXY)
    env['NO_PROXY'] = ','.join(sorted(no_proxy))
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)
    return env


def run_command(cmd, cwd=None, env=None, timeout=None, input_data=None):
    proc_env = clean_env(env)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=proc_env,
        stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **_popen_session_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc.pid)
        proc.communicate()
        raise HubError('TIMEOUT', f'Command timed out after {timeout}s')
    return proc.returncode, stdout, stderr


class ProcessRunner:
    def __init__(self, cmd, cwd=None, env=None):
        self.cmd = cmd
        self.cwd = cwd
        self.env = clean_env(env)
        self.proc = None
        self._win_job = None

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_popen_session_kwargs(),
        )
        if os.name == 'nt':
            try:
                from .windows_process import bind_process_to_job
                self._win_job = bind_process_to_job(self.proc.pid)
            except Exception:
                self._win_job = None
        return self.proc


class ProcessSupervisor:
    def __init__(self, runner):
        self.runner = runner
        self.tracked_pids = {}

    def monitor(self, timeout=None):
        proc = self.runner.start()
        start_time = time.monotonic()
        try:
            while proc.poll() is None:
                if timeout and (time.monotonic() - start_time) > timeout:
                    terminate_process_tree(proc.pid, self.tracked_pids)
                    raise HubError('TIMEOUT', 'Process execution exceeded timeout')
                if os.name != 'nt':
                    track_descendants(proc.pid, self.tracked_pids)
                time.sleep(0.1)
            return proc.returncode
        finally:
            if proc.poll() is None:
                terminate_process_tree(proc.pid, self.tracked_pids)
```

### Windows Job Object 绑定辅助 (`src/agent_hub/windows_process.py`)

```python
import subprocess

def bind_process_to_job(pid: int):
    """将 Windows 子进程绑定到 Kill-On-Close Job Object，防止孤儿进程残留。"""
    try:
        import win32api
        import win32con
        import win32job

        h_job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(h_job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(h_job, win32job.JobObjectExtendedLimitInformation, info)

        h_proc = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, pid)
        try:
            win32job.AssignProcessToJobObject(h_job, h_proc)
        finally:
            win32api.CloseHandle(h_proc)
        return h_job
    except Exception:
        return None


def terminate_win32_process_tree(pid: int) -> None:
    """终止 Windows 进程树（优先使用 taskkill /F /T，兜底使用 win32api）。"""
    try:
        subprocess.run(
            ['taskkill', '/F', '/T', '/PID', str(pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        try:
            import win32api
            import win32con
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
            try:
                win32api.TerminateProcess(handle, 1)
            finally:
                win32api.CloseHandle(handle)
        except Exception:
            pass
```

## 4. 有界 HTTP 线程池、长轮询条件唤醒与 Worktree 自动回收 (`src/agent_hub/daemon.py`)

### 优化点说明
- **环境变量化运行期参数**：将 `WORK_SESSION_LIMIT`、`WORKBENCH_PAGE`、`MAX_HTTP_THREADS` 和 `WORKTREE_TTL_SECONDS` 改为支持环境变量覆盖。
- **有界并发 HTTP Server (`HubHTTPServer`)**：使用 `threading.BoundedSemaphore(MAX_HTTP_THREADS)` 限制 `ThreadingHTTPServer` 的最大并发工作线程数，防止多路 `wait_for_task` 长轮询耗尽系统线程。
- **条件变量唤醒替代 0.2s 盲轮询**：通过 `service.event_condition`（`threading.Condition`）在新事件写入时立即通知长轮询线程，未触发时按最长 1.0s 步进等待，将空闲时的 SQLite 查询次数降低 80%。
- **定期清理过期 Git Worktree**：守护进程后台线程定期调用 `git worktree prune` 并回收已处于终态（`succeeded` / `failed` / `canceled`）且超过保留时长的临时 `hub/<task_id>` 分支与目录。

### 核心重构代码片段 (`src/agent_hub/daemon.py`)

```python
import os
import socketserver
import threading
import time
from http.server import ThreadingHTTPServer

WORK_SESSION_LIMIT = int(os.environ.get('AGENT_HUB_SESSION_LIMIT', '1024'))
WORKBENCH_PAGE = int(os.environ.get('AGENT_HUB_WORKBENCH_PAGE', '200'))
MAX_HTTP_THREADS = int(os.environ.get('AGENT_HUB_MAX_HTTP_THREADS', '64'))
WORKTREE_TTL_SECONDS = int(os.environ.get('AGENT_HUB_WORKTREE_TTL_SECONDS', '86400'))
LATEST_SEQ = 9223372036854775807


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, dashboard, max_threads=MAX_HTTP_THREADS):
        self.dashboard = dashboard
        self._thread_limiter = threading.BoundedSemaphore(max(8, max_threads))
        super().__init__(address, handler)

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]

    def process_request(self, request, client_address):
        if not self._thread_limiter.acquire(timeout=5.0):
            self.close_request(request)
            return
        t = threading.Thread(
            target=self._process_request_bounded,
            args=(request, client_address),
            daemon=True,
        )
        t.start()

    def _process_request_bounded(self, request, client_address):
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._thread_limiter.release()


def wait_for_events_long_poll(service, attempt_id: str, after: int, timeout_sec: float = 10.0):
    """使用 Condition 条件变量等待新事件，避免每 0.2 秒高频查询 SQLite。"""
    deadline = time.monotonic() + min(max(timeout_sec, 0.0), 10.0)
    condition = getattr(service, 'event_condition', None)
    while True:
        result = service.call('get_events', dict(task_id=attempt_id, after_seq=after, limit=WORKBENCH_PAGE))
        remaining = deadline - time.monotonic()
        if result.get('events') or remaining <= 0 or service.stopping.is_set():
            return result
        if condition is not None:
            with condition:
                condition.wait(timeout=min(remaining, 1.0))
        else:
            time.sleep(min(remaining, 0.5))
```

## 5. 插件加载白名单与类型安全加固 (`src/agent_hub/plugins.py`)

### 优化点说明
- **新增外部插件显式白名单 (`AGENT_HUB_ALLOWED_PLUGINS` / `allowed_plugins`)**：防止 Python 环境中意外安装的第三方包通过 `agent_hub.executors` entry-point 被无感知加载。
- **修复 `frozenset` 拼写与严格校验**：确保 `BUILTIN_NAMES` 与 `MODES` 使用标准内置 `frozenset`，并在 `load_plugin` 中校验插件名是否在受信任列表中。

### 完整替换代码 (`src/agent_hub/plugins.py`)

已单独导出为 `plugins.py` 文件，可直接覆盖 `src/agent_hub/plugins.py`。
