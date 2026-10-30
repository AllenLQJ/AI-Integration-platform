# Codex Execution Spec: AI-Integration-platform Optimization & Skill Integration

You are modifying the `AllenLQJ/AI-Integration-platform` repository. Execute the following 6 tasks in order, preserving all existing public function signatures and ensuring the entire `pytest` suite in `tests/` passes.

## Task 1: Add the `agent-hub-orchestrator` Skill File

Create `skills/agent-hub-orchestrator/SKILL.md` (and optionally copy to `.claude/skills/agent-hub-orchestrator/SKILL.md`) with the exact content below:

```markdown
---
name: agent-hub-orchestrator
description: Orchestrate multi-agent tasks across Codex, Claude Code, Muse, Cursor, Grok Bot, and Antigravity using the AI-Integration-platform (HubGateway v1) MCP tools. Use when the user asks to dispatch tasks to local or remote agents, manage work items, switch executors, handle isolated Git worktree edits, recover unknown remote task states, or resume browser tasks after human intervention.
---
# Agent Hub Orchestrator

Coordinate heterogeneous AI executors (Codex, Claude Code, Muse, Cursor, Grok Bot, and Antigravity) through the 20 standardized HubGateway v1 MCP tools exposed by `local-agent-hub`.

## When to Use

- Dispatching background coding, research, or browser automation tasks to local or remote executors via `submit_task`
- Coordinating multi-attempt or multi-agent workflows under a shared `work_item_id`
- Performing isolated code changes in a Git worktree (`edit_worktree` mode) and reviewing diffs before merging
- Handling human-in-the-loop pauses (`waiting_for_user`) for Muse browser login or CAPTCHA verification
- Switching executors mid-stream (`switch_executor`) when one agent stalls or needs another agent's specialty
- Recovering tasks stuck in `REMOTE_STATE_UNKNOWN` via `reconcile_task`

## Steps

### 1. Discover Available Executors and Capabilities

Before submitting the first task in a session, inspect registered executors and protocol features so you never pass an unsupported `mode` to an agent.

- Call `list_agents` to check which executors (`codex`, `claude`, `muse`, `cursor`, `cursor_cli`, `grokbot`, `antigravity`, or external plugins) are `available: true` and which modes (`text_only`, `read_only`, `edit_worktree`, `delegate`, `continue`) each supports.
- Call `get_capabilities` or `list_nodes` only when targeting distributed nodes or checking HubGateway v1 static limits.

### 2. Initialize a Work Item for Multi-Step or Multi-Agent Tasks

Use a Work Item container whenever a goal spans multiple attempts, requires acceptance verification, or may switch between agents.

- Call `create_work_item` with a clear title, goal description, and acceptance criteria to obtain a stable `work_item_id`.
- Call `claim_controller` with your `controller_id` before mutating or switching executors on a shared work item. Always pass the returned `controller_epoch` on subsequent controller operations to prevent split-brain writes.
- Use `link_task` if an earlier standalone `task_id` needs to be retroactively attached to the `work_item_id`.

### 3. Submit the Task with the Appropriate Isolation Mode

Choose the least-privileged `mode` that satisfies the task requirements when calling `submit_task`:

- Use `text_only` for pure reasoning or Q&A where file reads and shell commands are unnecessary.
- Use `read_only` for codebase exploration, architecture review, and bug diagnosis without modifying files.
- Use `edit_worktree` with `auto_start_worktree: true` and a valid Git repository `cwd` for any code modification task. The daemon automatically creates an isolated `hub/<task_id>` Git worktree so concurrent edits never corrupt the user's working tree.
- Use `delegate` for browser automation (`muse`) or remote bot workflows (`grokbot`).
- Record key orchestration milestones on the shared timeline using `record_activity` so the Web Dashboard stays informative.

### 4. Wait for Completion and Stream Events Efficiently

Avoid tight polling loops with `get_task` because rapid polling wastes tokens and increases SQLite contention.

- Call `wait_for_task` (which performs a 0 to 10 second server-side long poll) until the task reaches a terminal state (`succeeded`, `failed`, `canceled`) or pauses at `waiting_for_user`.
- If the task is still `running` after `wait_for_task` returns, optionally call `get_events` with the last seen `after_seq` cursor (up to `limit: 200`) to inspect incremental progress, then call `wait_for_task` again.
- Once the task reaches `succeeded`, call `get_result` to retrieve the final `answer`, `git diff`, `artifacts`, and `source_urls`.

### 5. Handle Human-in-the-Loop Browser Interventions (`waiting_for_user`)

When a browser executor like `muse` encounters a login wall, 2FA prompt, or CAPTCHA, `wait_for_task` returns `state: "waiting_for_user"` along with `needs_user: true` and an `intervention_id`.

- Stop polling immediately and present the prompt or screenshot artifact (retrieved via `get_artifact`) to the user.
- Wait for the user to confirm they have completed the action in the browser window.
- Call `resume_task` passing the `task_id` and `intervention_id`, then resume monitoring with `wait_for_task`.

### 6. Continue Sessions, Switch Executors, or Verify Results

After an attempt finishes, decide whether to iterate in-place, hand off to another agent, or finalize verification:

- **Same-agent follow-up**: If the executor supports `continue` and returned a valid `session_id`, call `continue_task` to send follow-up instructions while preserving the native session context and worktree.
- **Cross-agent handoff**: If the current attempt failed or requires a different model (for example, handing off from `claude` architectural planning to `codex` implementation), call `switch_executor` on the `work_item_id` to spawn a fresh attempt under the new agent while preserving work item history.
- **Verification**: For `edit_worktree` tasks, call `verify_task` to validate that the worktree diff and output artifacts remain intact and idempotent before presenting the final result.
- **Remote state recovery**: If a remote executor task reports `REMOTE_STATE_UNKNOWN`, call `reconcile_task` before retrying so you never duplicate an unconfirmed remote submission.

## Gotchas

- **Never tight-loop `get_task`**: Always prefer `wait_for_task` for blocking waits. Use `get_task` only for non-blocking status snapshots across multiple tasks.
- **Never retry `REMOTE_STATE_UNKNOWN` blindly**: Remote executors (`grokbot`, remote sessions) may still be running. Always call `reconcile_task` first to probe remote state.
- **Respect `continue` capability boundaries**: `switch_executor` intentionally starts a clean native session on the target agent while inheriting Work Item context; do not pass an old agent's native `session_id` to a different executor adapter.
- **Clean cancellation**: When aborting a runaway task, call `cancel_task` and verify the state transitions to `canceled` before starting a replacement attempt in the same directory.
```

## Task 2: Relax Strict Dependency Pins in `pyproject.toml`

Update `pyproject.toml` so `mcp` and `httpx` use compatible semver ranges (`mcp>=1.30.0,<2.0.0`, `httpx>=0.28.1,<1.0.0`) rather than `==` pins, and add a `dev` optional dependency group for `pytest`:

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

## Task 3: Harden SQLite Connection PRAGMAs & Write Serialization in `src/agent_hub/store.py`

Inspect `src/agent_hub/store.py` and apply the following changes while keeping all existing methods on `Store` intact:

1. Add `DEFAULT_BUSY_TIMEOUT_MS = int(os.environ.get("AGENT_HUB_SQLITE_BUSY_TIMEOUT_MS", "5000"))` and initialize `self._write_lock = threading.RLock()` in `Store.__init__`.
2. In `Store.connection(self, write: bool = False)`, ensure every newly opened `sqlite3.Connection` executes:
   - `PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};`
   - `PRAGMA synchronous = NORMAL;`
   - `PRAGMA foreign_keys = ON;`
3. When `write=True` (or when serializing concurrent writes under `ThreadingHTTPServer`), guard the transaction with `self._write_lock` to prevent `sqlite3.OperationalError: database is locked`.

## Task 4: Fix Cross-Platform Process Tree Isolation in `src/agent_hub/processes.py` & `src/agent_hub/windows_process.py`

Inspect `src/agent_hub/processes.py` and `src/agent_hub/windows_process.py` and apply the following changes while preserving existing function/class signatures:

1. In `src/agent_hub/processes.py`:
   - Ensure `subprocess.Popen` in `run_command` and `ProcessRunner.start` passes `start_new_session=True` on POSIX (`os.name != 'nt'`) and `creationflags= getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)` on Windows so child processes do not share the daemon's own process group.
   - In `terminate_process_tree(pid, ...)`, check `daemon_pgid = os.getpgrp()` before calling `os.killpg(pgid, ...)` so the daemon never signals its own process group. Wait with a bounded polling loop up to `graceful_timeout` before sending `SIGKILL`, and also terminate any tracked detached descendants via `stop_descendants`.
   - In `run_command`, catch `subprocess.TimeoutExpired`, call `terminate_process_tree(proc.pid)`, and raise `HubError('TIMEOUT', ...)`.
2. In `src/agent_hub/windows_process.py`:
   - Add a helper `bind_process_to_job(pid)` using `win32job.CreateJobObject` and `win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (falling back gracefully if `pywin32` is unavailable or permission is denied) so Windows child trees are automatically reaped if the daemon exits.

## Task 5: Bound HTTP Thread Creation & Optimize Long-Polling in `src/agent_hub/daemon.py`

Inspect `src/agent_hub/daemon.py` and apply the following improvements without breaking existing HTTP endpoints or `tests/test_daemon.py`:

1. Make runtime constants configurable via environment variables with backwards-compatible defaults:
   - `WORK_SESSION_LIMIT = int(os.environ.get('AGENT_HUB_SESSION_LIMIT', '1024'))`
   - `WORKBENCH_PAGE = int(os.environ.get('AGENT_HUB_WORKBENCH_PAGE', '200'))`
   - `MAX_HTTP_THREADS = int(os.environ.get('AGENT_HUB_MAX_HTTP_THREADS', '64'))`
2. In `HubHTTPServer`, set `daemon_threads = True` and guard thread spawning in `process_request` with `threading.BoundedSemaphore(max(8, MAX_HTTP_THREADS))` so burst long-polling requests cannot exhaust OS threads.
3. In the 10-second `get_events` / `wait_for_task` long-polling loop, if `service` exposes an event notification condition (`threading.Condition`), wait on it instead of unconditional `time.sleep(0.2)` busy-polling.

## Task 6: Add Plugin Allowlist & Path Traversal Guard in `src/agent_hub/plugins.py`

Update `src/agent_hub/plugins.py` with the following implementation (preserves all v1 SDK contracts while adding `AGENT_HUB_ALLOWED_PLUGINS` allowlist support and verifying `PluginContext.data_dir` stays inside the Hub root):

```python
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


def _env_allowed_plugins():
    raw = os.environ.get('AGENT_HUB_ALLOWED_PLUGINS', '').strip()
    if not raw:
        return None
    if raw == '*':
        return '*'
    return {name.strip() for name in raw.split(',') if name.strip()}


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
        return dict(
            enforced_by='executor',
            enforced=False,
            allows_write=None,
            allows_commands=None,
            os_sandbox=False,
            scope='Plugin runs as the current OS user; Hub does not enforce tool permissions.',
        )


def validate_instance_name(name):
    if (
        not isinstance(name, str)
        or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name)
        or name in _DEVICE_NAMES
        or name in BUILTIN_NAMES
        or name.startswith('hub-')
    ):
        raise HubError('INVALID_CONFIG', 'Plugin/command instance name must be portable and not reserved')


def plugin_context(name, root):
    validate_instance_name(name)
    root_path = Path(root).resolve()
    target_dir = (root_path / 'plugins' / name).resolve()
    if root_path not in target_dir.parents:
        raise HubError('INVALID_CONFIG', 'Plugin data directory escaped hub root')
    return PluginContext(name, target_dir)


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
    if result.get('session_id') is not None and (
        not isinstance(result['session_id'], str) or not result['session_id'].strip()
    ):
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
        if (
            not isinstance(caps, (list, tuple))
            or not caps
            or any(not isinstance(c, str) or c not in MODES for c in caps)
            or len(set(caps)) != len(caps)
            or not set(caps) - {'continue'}
        ):
            _contract('Executor must declare unique supported modes')
        self.capabilities = list(caps)
        self.remote_cancel = getattr(executor, 'remote_cancel', None)
        self.cancel_scope = getattr(executor, 'cancel_scope', None)
        if type(self.remote_cancel) is not bool or self.cancel_scope not in {
            'local_process_tree', 'remote_session', 'remote_hub_task'
        }:
            _contract('Executor must declare cancellation support and scope')
        for method in ('diagnose', 'run', 'permission_policy'):
            if (
                not callable(getattr(executor, method, None))
                or getattr(type(executor), method, None) is getattr(Executor, method, None)
            ):
                _contract('Executor must implement ' + method)
        self._permissions = {}
        for mode in self.capabilities:
            if mode == 'continue':
                continue
            policy = executor.permission_policy(mode)
            if (
                not isinstance(policy, dict)
                or type(policy.get('enforced')) is not bool
                or not isinstance(policy.get('enforced_by'), str)
                or not policy['enforced_by']
                or any(
                    value is not None and type(value) is not bool
                    for value in (policy.get('allows_write'), policy.get('allows_commands'))
                )
                or type(policy.get('os_sandbox')) is not bool
            ):
                _contract('Executor permission policy is incomplete or invalid')
            if mode in {'read_only', 'text_only'} and (
                not policy['enforced'] or policy.get('allows_write') is not False
            ):
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
        return dict(
            result,
            plugin=self.plugin,
            plugin_api_version=API_VERSION,
            capabilities=list(self.capabilities),
            remote_cancel=self.remote_cancel,
            cancel_scope=self.cancel_scope,
            permissions={m: dict(p) for m, p in self._permissions.items()},
        )

    def run(self, task, cancel, event):
        if task.get('resume_session') and 'continue' not in self.capabilities:
            raise HubError('UNSUPPORTED_MODE', 'Executor does not support continuation')
        try:
            result = self._executor.run(task, cancel, event)
        except HubError as error:
            if not isinstance(error.code, str) or not error.code:
                _contract('Executor error code must be a nonempty string')
            if not isinstance(error.details, dict) or (
                'artifacts' in error.details and not isinstance(error.details['artifacts'], dict)
            ):
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

    allowlist = allowed_plugins if allowed_plugins is not None else _env_allowed_plugins()
    if allowlist is not None and allowlist != '*' and plugin not in allowlist:
        raise HubError(
            'PLUGIN_NOT_ALLOWED',
            f'Plugin {plugin} is not in AGENT_HUB_ALLOWED_PLUGINS allowlist',
        )

    matches = [entry for entry in metadata.entry_points(group=ENTRY_POINT_GROUP) if entry.name == plugin]
    if not matches:
        raise HubError('PLUGIN_NOT_FOUND', 'Install the executor plugin in the Hub Python environment: ' + plugin)
    if len(matches) != 1:
        raise HubError('INVALID_CONFIG', 'Multiple installed distributions provide plugin: ' + plugin)
    try:
        descriptor = matches[0].load()
        if (
            not isinstance(descriptor, ExecutorPlugin)
            or type(descriptor.api_version) is not int
            or descriptor.api_version != API_VERSION
            or not callable(descriptor.create)
        ):
            _contract('Plugin must export ExecutorPlugin(api_version=1, create=factory)')
        executor = descriptor.create(config=dict(options), context=context)
        return ValidatedExecutor(executor, plugin)
    except HubError:
        raise
    except Exception as error:
        raise HubError('PLUGIN_LOAD_FAILED', f'Plugin {plugin} failed to load ({type(error).__name__})') from None


def installed_plugins():
    """Read metadata only: never import third-party modules during discovery."""
    result = []
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        distribution = getattr(entry, 'dist', None)
        result.append(
            dict(
                name=entry.name,
                distribution=distribution.metadata.get('Name') if distribution else None,
                version=distribution.version if distribution else None,
            )
        )
    return result
```

## Verification Checklist

After applying all changes, run the test suite and syntax checks:

1. Run `python -m py_compile src/agent_hub/*.py` and confirm zero syntax errors.
2. Run `pytest` across `tests/` (`test_adapters.py`, `test_command_adapter.py`, `test_antigravity.py`, `test_daemon.py`, `test_activity.py`, `test_dashboard.py`, `test_dashboard_http.py`, `test_dashboard_chains.py`) and ensure all tests pass.
3. Run `git diff` and `git status` to verify all modifications are clean and self-contained.
