"""Regression coverage for runtime isolation, concurrency and plugin boundaries."""
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent_hub import processes
from agent_hub.models import HubError
from agent_hub.plugins import load_plugin, plugin_context
from agent_hub.store import Store


@pytest.mark.parametrize('failure', [None, 'configure', 'open', 'assign'])
def test_windows_bind_job_closes_handles_on_failure(monkeypatch, failure):
    from agent_hub import windows_process
    job, process = Mock(), Mock()
    api = SimpleNamespace(OpenProcess=Mock(return_value=process))
    jobs = SimpleNamespace(
        CreateJobObject=Mock(return_value=job),
        QueryInformationJobObject=Mock(return_value={'BasicLimitInformation': {'LimitFlags': 0}}),
        SetInformationJobObject=Mock(), AssignProcessToJobObject=Mock(),
        JobObjectExtendedLimitInformation=9, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192)
    if failure == 'configure':
        jobs.SetInformationJobObject.side_effect = OSError('denied')
    if failure == 'open':
        api.OpenProcess.side_effect = OSError('denied')
    if failure == 'assign':
        jobs.AssignProcessToJobObject.side_effect = OSError('denied')
    for name, module in {'win32api': api, 'win32job': jobs,
                         'win32con': SimpleNamespace(PROCESS_SET_QUOTA=256, PROCESS_TERMINATE=1)}.items():
        monkeypatch.setitem(sys.modules, name, module)
    result = windows_process.bind_process_to_job(123)
    if failure:
        assert result is None
        job.Close.assert_called_once()
    else:
        assert result is job
        job.Close.assert_not_called()
        info = jobs.SetInformationJobObject.call_args.args[2]
        assert info['BasicLimitInformation']['LimitFlags'] & 8192
    if failure in (None, 'assign'):
        process.Close.assert_called_once()


@pytest.mark.parametrize('pooled', [False, True])
def test_connection_pragmas_and_rollback(tmp_path, pooled):
    store = Store(tmp_path / 'tasks.db')
    if pooled:
        store.reuse_connections()
    try:
        for _ in range(2):
            with store.connection() as db:
                assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
                assert db.execute('PRAGMA synchronous').fetchone()[0] == 1
                assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with pytest.raises(ValueError):
            with store.connection(write=True) as db:
                db.execute("INSERT INTO reconcile_requests VALUES ('a','d','t','{}')")
                raise ValueError('rollback')
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM reconcile_requests').fetchone()[0] == 0
    finally:
        store.close()


def test_store_writers_are_serialized_without_blocking_readers(tmp_path):
    store = Store(tmp_path / 'tasks.db')
    task = store.create('a', {})
    entered = threading.Event()
    def write():
        entered.set()
        store.event(task['id'], 'message', {'value': 'committed'})
    with ThreadPoolExecutor(2) as pool:
        with store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            future = pool.submit(write)
            assert entered.wait(1)
            assert pool.submit(store.get, task['id']).result(1)['id'] == task['id']
            assert not future.done()
        future.result(2)
    assert store.events(task['id'])['events'][-1]['kind'] == 'message'


def test_concurrent_writers_with_zero_sqlite_busy_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr('agent_hub.store.DEFAULT_BUSY_TIMEOUT_MS', 0)
    store = Store(tmp_path / 'tasks.db')
    task = store.create('a', {})
    with ThreadPoolExecutor(12) as pool:
        list(pool.map(lambda n: store.event(task['id'], 'progress', {'n': n}), range(100)))
    events = store.events(task['id'], limit=200)['events']
    assert len(events) == 101
    assert {e['data']['n'] for e in events[1:]} == set(range(100))


def test_write_notifications_only_follow_commit_and_do_not_lose_wakeup(tmp_path):
    store = Store(tmp_path / 'tasks.db')
    before = store.change_token()
    with pytest.raises(ValueError):
        with store.connection(write=True) as db:
            db.execute("INSERT INTO reconcile_requests VALUES ('bad','d','t','{}')")
            raise ValueError()
    assert store.change_token() == before
    store.create('ok', {})
    # Commit happened BEFORE waiting; the generation must prevent a lost wakeup.
    assert store.wait_for_change(before, 0)


def test_posix_termination_never_signals_daemon_group(monkeypatch):
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    proc = Mock(pid=42)
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired('child', 2), 0]
    with patch.object(processes.os, 'name', 'posix'), \
            patch.object(processes.os, 'getpgrp', return_value=42, create=True), \
            patch.object(processes.os, 'getpgid', return_value=42, create=True), \
            patch.object(processes.os, 'killpg', create=True) as group, \
            patch.object(processes.os, 'kill') as single:
        del proc.stop_tree
        processes.terminate_tree(proc)
    group.assert_not_called()
    assert single.call_count == 2


def test_missing_ps_cleanup_does_not_kill_daemon_group(monkeypatch):
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    with patch.object(processes, 'posix_processes', return_value=None), \
            patch.object(processes.os, 'getpgrp', return_value=42, create=True), \
            patch.object(processes.os, 'killpg', create=True) as group:
        processes.stop_descendants({}, root_group=42)
    group.assert_not_called()


def test_plugin_env_allowlist_blocks_before_import(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HUB_ALLOWED_PLUGINS', 'allowed, another')
    with patch('agent_hub.plugins.metadata.entry_points') as discovery:
        with pytest.raises(HubError) as caught:
            load_plugin('test-agent', {'plugin': 'allow'}, tmp_path)
    assert caught.value.code == 'PLUGIN_NOT_ALLOWED'
    discovery.assert_not_called()


def test_plugin_explicit_empty_allowlist_denies_even_with_wildcard_env(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HUB_ALLOWED_PLUGINS', '*')
    with pytest.raises(HubError) as caught:
        load_plugin('test-agent', {'plugin': 'echo'}, tmp_path, allowed_plugins=[])
    assert caught.value.code == 'PLUGIN_NOT_ALLOWED'


def test_plugin_directory_cannot_escape_via_link(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'hub'
    root.mkdir()
    try:
        (root / 'plugins').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Creating directory symlinks requires OS permission')
    with pytest.raises(HubError) as caught:
        plugin_context('test-agent', root)
    assert caught.value.code == 'INVALID_CONFIG'


def test_http_worker_limit_and_failed_start_release_permits():
    from agent_hub import daemon
    dashboard = Mock()
    with patch.object(daemon, 'MAX_HTTP_THREADS', 8, create=True):
        server = daemon.HubHTTPServer(('127.0.0.1', 0), Mock(), dashboard)
    try:
        requests = [Mock() for _ in range(9)]
        with patch.object(threading.Thread, 'start') as start:
            for request in requests:
                server.process_request(request, ('127.0.0.1', 1))
            assert start.call_count == 8
            requests[-1].close.assert_called_once()
        for request in requests[:8]:
            with patch.object(server, 'process_request_thread'):
                server._process_request_bounded(request, ('127.0.0.1', 1))
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('start failed')):
            with pytest.raises(RuntimeError):
                server.process_request(Mock(), ('127.0.0.1', 1))
        for _ in range(8):
            assert server._thread_limiter.acquire(blocking=False)
        assert not server._thread_limiter.acquire(blocking=False)
        for _ in range(8):
            server._thread_limiter.release()
    finally:
        server.server_close()


def test_wait_for_task_idle_uses_few_snapshots(tmp_path):
    from agent_hub.service import Service
    service = Service(tmp_path, {})
    try:
        service.stopping.set()
        service.thread.join(2)
        service.stopping.clear()
        task = service.store.create('unschedulable', {'agent': 'missing'})
        with patch.object(service.store, 'wait_snapshot', wraps=service.store.wait_snapshot) as read:
            result = service.wait_for_task(task['id'], timeout_seconds=.35)
        assert result['timed_out']
        assert read.call_count <= 2
    finally:
        service.close()
