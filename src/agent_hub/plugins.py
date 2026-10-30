"""Public v1 SDK and explicit loading of trusted executor distributions.

Plugins run as the Hub user. Validation is a compatibility boundary, not a sandbox.
Installing a distribution alone never opts it into execution.
"""
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable
import json
import os
import re

from .models import HubError

API_VERSION = 1
ENTRY_POINT_GROUP = 'agent_hub.executors'
BUILTIN_NAMES = frozenset({'codex', 'claude', 'muse', 'cursor', 'cursor_cli', 'grokbot', 'antigravity'})
MODES = frozenset({'text_only', 'read_only', 'edit_worktree', 'delegate', 'continue'})
_DEVICE_NAMES = {'con', 'prn', 'aux', 'nul'} | {f'{prefix}{i}' for prefix in ('com', 'lpt') for i in range(1, 10)}


@dataclass(frozen=True)
class PluginContext:
    name: str
    data_dir: Path


@dataclass(frozen=True)
class ExecutorPlugin:
    api_version: int
    create: Callable


class Executor:
    """Conservative defaults; override methods/capabilities your tool implements.

    run(task, cancel, event) must honor cancellation/deadline and return a JSON
    object with a string answer. Never retry an unconfirmed remote submission.
    State belongs in PluginContext.data_dir, not the package/source directory.
    """
    capabilities = ['delegate']
    remote_cancel = False
    cancel_scope = 'remote_session'

    def diagnose(self):
        raise NotImplementedError

    def run(self, task, cancel, event):
        raise NotImplementedError

    def permission_policy(self, mode):
        return dict(enforced_by='executor', enforced=False, allows_write=None,
                    allows_commands=None, os_sandbox=False,
                    scope='Plugin runs as the current OS user; Hub does not enforce tool permissions.')


def validate_instance_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name)
            or name in _DEVICE_NAMES or name in BUILTIN_NAMES or name.startswith('hub-')):
        raise HubError('INVALID_CONFIG', 'Plugin/command instance name must be portable and not reserved')


def plugin_context(name, root):
    validate_instance_name(name)
    root = Path(root).resolve()
    target = (root / 'plugins' / name).resolve()
    if root not in target.parents:
        raise HubError('INVALID_CONFIG', 'Plugin data directory escaped hub root')
    return PluginContext(name, target)


def _contract(message):
    raise HubError('PLUGIN_CONTRACT_ERROR', message)


def _validate_json(value):
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        _contract('Executor data must be finite, acyclic JSON')
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                _contract('JSON object keys must be strings')
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item is not None and type(item) not in (str, bool, int, float):
            _contract('Executor data must use JSON types (objects, arrays and scalar values)')


def validate_result(result):
    if not isinstance(result, dict) or not isinstance(result.get('answer'), str):
        _contract('Executor result must be an object with string answer')
    if result.get('session_id') is not None and (not isinstance(result['session_id'], str) or not result['session_id']):
        _contract('session_id must be a nonempty string or null')
    for key, cls in (('artifacts', dict), ('media_artifacts', list), ('source_urls', list)):
        if key in result and not isinstance(result[key], cls):
            _contract(key + ' has invalid type')
    if any(not isinstance(url, str) for url in result.get('source_urls', [])):
        _contract('source_urls must contain strings')
    _validate_json(result)
    return result


class ValidatedExecutor:
    """Cache validated declarations; normalize metadata without exposing config."""
    api_version = API_VERSION

    def __init__(self, executor, plugin):
        self._executor, self.plugin = executor, plugin
        caps = getattr(executor, 'capabilities', None)
        if (not isinstance(caps, (list, tuple)) or not caps
                or any(not isinstance(c, str) or c not in MODES for c in caps)
                or len(set(caps)) != len(caps) or not set(caps) - {'continue'}):
            _contract('Executor must declare unique supported modes')
        self.capabilities = list(caps)
        self.remote_cancel = getattr(executor, 'remote_cancel', None)
        self.cancel_scope = getattr(executor, 'cancel_scope', None)
        if type(self.remote_cancel) is not bool or self.cancel_scope not in {
                'local_process_tree', 'remote_session', 'remote_hub_task'}:
            _contract('Executor must declare cancellation support and scope')
        for method in ('diagnose', 'run', 'permission_policy'):
            if not callable(getattr(executor, method, None)) or getattr(type(executor), method, None) is getattr(Executor, method, None) and method != 'permission_policy':
                _contract('Executor must implement ' + method)
        self._permissions = {}
        for mode in self.capabilities:
            if mode == 'continue':
                continue
            policy = executor.permission_policy(mode)
            if (not isinstance(policy, dict) or type(policy.get('enforced')) is not bool
                    or not isinstance(policy.get('enforced_by'), str) or not policy['enforced_by']
                    or any(value is not None and type(value) is not bool
                           for value in (policy.get('allows_write'), policy.get('allows_commands')))
                    or type(policy.get('os_sandbox')) is not bool):
                _contract('Executor permission policy is incomplete or invalid')
            if mode in {'read_only', 'text_only'} and (not policy['enforced'] or policy.get('allows_write') is not False):
                _contract(mode + ' requires an enforced no-write policy')
            if mode == 'text_only' and policy.get('allows_commands') is not False:
                _contract('text_only requires commands disabled')
            _validate_json(policy)
            self._permissions[mode] = dict(policy)

    def permission_policy(self, mode):
        if mode not in self._permissions:
            raise HubError('UNSUPPORTED_MODE', 'Executor does not support ' + str(mode))
        return dict(self._permissions[mode])

    def diagnose(self):
        result = self._executor.diagnose()
        if not isinstance(result, dict) or type(result.get('available')) is not bool:
            _contract('diagnose must return an object with boolean available')
        _validate_json(result)
        return dict(result, plugin=self.plugin, plugin_api_version=API_VERSION,
                    capabilities=list(self.capabilities), remote_cancel=self.remote_cancel,
                    cancel_scope=self.cancel_scope,
                    permissions={m: dict(p) for m, p in self._permissions.items()})

    def run(self, task, cancel, event):
        if task.get('resume_session') and 'continue' not in self.capabilities:
            raise HubError('UNSUPPORTED_MODE', 'Executor does not support continuation')
        try:
            result = self._executor.run(task, cancel, event)
        except HubError as error:
            if not isinstance(error.code, str) or not error.code:
                _contract('Executor error code must be a nonempty string')
            if not isinstance(error.details, dict) or ('artifacts' in error.details and not isinstance(error.details['artifacts'], dict)):
                _contract('Executor error details must be an object with object artifacts')
            _validate_json(error.details)
            raise
        return validate_result(result)


def load_plugin(name, item, root, allowed_plugins=None):
    context = plugin_context(name, root)
    plugin = item.get('plugin')
    options = item.get('options', {})
    if not isinstance(plugin, str) or not plugin.strip() or not isinstance(options, dict):
        raise HubError('INVALID_CONFIG', 'Plugin requires a nonempty plugin name and object options')
    if allowed_plugins is None:
        raw = os.environ.get('AGENT_HUB_ALLOWED_PLUGINS', '').strip()
        allowed_plugins = None if not raw else '*' if raw == '*' else {
            name.strip() for name in raw.split(',') if name.strip()}
    if allowed_plugins is not None and allowed_plugins != '*':
        if (not isinstance(allowed_plugins, (list, tuple, set, frozenset))
                or any(not isinstance(name, str) for name in allowed_plugins)):
            raise HubError('INVALID_CONFIG', 'allowed_plugins must be a collection of plugin names or *')
        if plugin not in allowed_plugins:
            raise HubError('PLUGIN_NOT_ALLOWED', 'Plugin is not in AGENT_HUB_ALLOWED_PLUGINS allowlist: ' + plugin)
    matches = [entry for entry in metadata.entry_points(group=ENTRY_POINT_GROUP) if entry.name == plugin]
    if not matches:
        raise HubError('PLUGIN_NOT_FOUND', 'Install the executor plugin in the Hub Python environment: ' + plugin)
    if len(matches) != 1:
        raise HubError('INVALID_CONFIG', 'Multiple installed distributions provide plugin: ' + plugin)
    try:
        descriptor = matches[0].load()
        if (not isinstance(descriptor, ExecutorPlugin) or type(descriptor.api_version) is not int
                or descriptor.api_version != API_VERSION or not callable(descriptor.create)):
            _contract('Plugin must export ExecutorPlugin(api_version=1, create=factory)')
        executor = descriptor.create(config=dict(options), context=context)
        return ValidatedExecutor(executor, plugin)
    except HubError:
        raise
    except Exception as error:
        # A plugin may put credentials in exception strings; only report its type.
        raise HubError('PLUGIN_LOAD_FAILED', f'Plugin {plugin} failed to load ({type(error).__name__})') from None


def installed_plugins():
    """Read metadata only: never import third-party modules during discovery."""
    result = []
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        distribution = getattr(entry, 'dist', None)
        result.append(dict(name=entry.name,
                           distribution=distribution.metadata.get('Name') if distribution else None,
                           version=distribution.version if distribution else None))
    return sorted(result, key=lambda item: (item['name'], item['distribution'] or ''))
