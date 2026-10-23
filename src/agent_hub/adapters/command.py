"""Portable, shell-free bridge to external tools via a short-lived JSON request."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from .base import executable_available, resolve_executable
from ..models import HubError
from ..plugins import Executor, validate_result
from ..processes import SpawnError, environment, run_process
from ..review import strict_json

SCHEMA = 'agent-hub.command.v1'
MAX_RESPONSE_CHARS = 1024 * 1024
_TOKEN = re.compile(r'\{([a-z_]+)\}')
_TOKENS = {'request_file', 'prompt', 'workspace', 'session_id', 'mode'}
_FIELDS = {'executable', 'args', 'resume_args', 'result_format', 'modes', 'env_from'}


def _arguments(value):
    if (not isinstance(value, list) or not value or any(not isinstance(a, str) or '\0' in a for a in value)
            or any(token not in _TOKENS for arg in value for token in _TOKEN.findall(arg))):
        raise HubError('INVALID_CONFIG', 'Command args must be a nonempty string array with supported placeholders')
    return list(value)


class Command(Executor):
    def __init__(self, config, context):
        if not isinstance(config, dict) or set(config) - _FIELDS:
            raise HubError('INVALID_CONFIG', 'Unknown command setting; use env_from for private environment references')
        self.context = context
        self.executable = config.get('executable', sys.executable)
        if not isinstance(self.executable, str) or not self.executable.strip() or '\0' in self.executable:
            raise HubError('INVALID_CONFIG', 'Command executable must be a nonempty string')
        self.args = _arguments(config.get('args'))
        self.resume_args = _arguments(config['resume_args']) if 'resume_args' in config else None
        self.result_format = config.get('result_format', 'json')
        if self.result_format not in {'json', 'text'}:
            raise HubError('INVALID_CONFIG', 'Command result_format must be json or text')
        modes = config.get('modes', ['delegate'])
        if (not isinstance(modes, list) or not modes
                or any(not isinstance(m, str) or m not in {'delegate', 'edit_worktree'} for m in modes)
                or len(set(modes)) != len(modes)):
            raise HubError('INVALID_CONFIG', 'Command modes may only contain delegate and edit_worktree')
        if self.resume_args is not None and self.result_format != 'json':
            raise HubError('INVALID_CONFIG', 'Command continuation requires json results with session_id')
        self.capabilities = list(modes) + (['continue'] if self.resume_args is not None else [])
        self.env_from = config.get('env_from', {})
        if (not isinstance(self.env_from, dict) or any(
                not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
                or not isinstance(v, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v)
                for k, v in self.env_from.items())):
            raise HubError('INVALID_CONFIG', 'env_from must map environment names to environment names')

    def _environment(self):
        missing = [source for source in self.env_from.values() if source not in os.environ]
        extra = {key: os.environ[source] for key, source in self.env_from.items() if source in os.environ}
        return environment(extra), missing

    def diagnose(self):
        env, missing = self._environment()
        resolved = resolve_executable(self.executable, env)
        return dict(available=executable_available(resolved) and not missing,
                    executable_spec=self.executable, resolved_executable=resolved,
                    missing_environment=missing, authentication='Actual task required to verify authentication',
                    capabilities=list(self.capabilities), remote_cancel=False, cancel_scope=self.cancel_scope,
                    permissions={m: self.permission_policy(m) for m in self.capabilities if m != 'continue'})

    def run(self, task, cancel, event):
        if task.get('mode') not in self.capabilities:
            raise HubError('UNSUPPORTED_MODE', 'Command does not support requested mode')
        if task.get('resume_session') and self.resume_args is None:
            raise HubError('UNSUPPORTED_MODE', 'Command has no resume_args')
        if cancel.is_set() or time.time() >= task['deadline']:
            raise HubError('CANCELLED' if cancel.is_set() else 'TIMED_OUT', 'Command was not started')
        env, missing = self._environment()
        if missing:
            raise HubError('AGENT_UNAVAILABLE', 'Missing environment variables: ' + ', '.join(missing))
        resolved = resolve_executable(self.executable, env)
        if not executable_available(resolved):
            raise HubError('AGENT_UNAVAILABLE', 'Command executable cannot be resolved')
        log_dir = Path(task['log_dir'])
        log_dir.mkdir(parents=True, exist_ok=True)
        self.context.data_dir.mkdir(parents=True, exist_ok=True)
        request = dict(schema=SCHEMA, task_id=task['id'], prompt=task['prompt'], mode=task['mode'],
                       workspace=task['workspace'], deadline=task['deadline'],
                       resume_session=task.get('resume_session'), context=task.get('context'),
                       plugin_data_dir=str(self.context.data_dir))
        fd, filename = tempfile.mkstemp(prefix='request-', suffix='.json', dir=log_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(request, output, ensure_ascii=False, allow_nan=False)
            values = dict(request_file=filename, prompt=task['prompt'], workspace=task['workspace'],
                          session_id=task.get('resume_session') or '', mode=task['mode'])
            args = self.resume_args if task.get('resume_session') else self.args
            # Single substitution pass: values are never interpreted as further templates.
            argv = [resolved] + [_TOKEN.sub(lambda match: values[match[1]], arg) for arg in args]
            captured, size, oversized = [], 0, False
            def capture(line):
                nonlocal size, oversized
                line = line.replace('\r\n', '\n')
                if self.result_format == 'json':
                    if not line.strip():
                        return
                    captured.clear()
                    size = 0
                    oversized = False
                size += len(line)
                if size > MAX_RESPONSE_CHARS:
                    oversized = True
                    captured.clear()
                elif not oversized:
                    captured.append(line)
            try:
                code, stdout, stderr, logs = run_process(argv, task['workspace'], env, cancel,
                    lambda: task['deadline'], log_dir, event, allow_hub_events=False,
                    parse_cli_events=False, stdout_line=capture)
            except SpawnError as error:
                raise HubError('AGENT_UNAVAILABLE', 'Command process could not start (' + type(error.cause).__name__ + ')') from None
            except HubError as error:
                if error.code in {'CANCELLED', 'TIMED_OUT'}:
                    error.details.update(remote_cancelled=False, local_process_stopped=True,
                                         cancel_scope=self.cancel_scope)
                raise
            if code:
                raise HubError('CLI_FAILED', 'Command exited unsuccessfully',
                               {'exit_code': code, 'artifacts': {'logs': logs}})
            if oversized:
                raise HubError('INVALID_RESULT', 'Command response exceeds 1 Mi characters', {'artifacts': {'logs': logs}})
            original = ''.join(captured)
            if self.result_format == 'text':
                result = {'answer': original.rstrip('\r\n')}
            else:
                try:
                    result = strict_json(original) if original.strip() else None
                    if not isinstance(result, dict) or set(result) - {'answer', 'session_id', 'source_urls'}:
                        raise ValueError('Unexpected response fields')
                    validate_result(result)
                except (ValueError, HubError):
                    raise HubError('INVALID_RESULT', 'Last stdout line must be a valid command response object',
                                   {'artifacts': {'logs': logs}}) from None
            return dict(result, exit_code=code, artifacts={'logs': logs})
        finally:
            Path(filename).unlink(missing_ok=True)
