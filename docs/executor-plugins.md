# Portable executor plugins (Hub 0.3, plugin API v1)

Hub accepts new tools without modifying core code through **installed Python plugins** or a **configured command bridge**. API, MCP, CLI and GUI tools still need a provider-specific adapter; this does not make an undocumented website automatically controllable. Capabilities differ by tool and must be declared honestly.

## Source, device configuration and runtime data

- Distribute source or built wheels. Do not distribute your virtual environment or private Hub data directory.
- Default private state is `~/.agent-hub` (`%USERPROFILE%\.agent-hub` on Windows). `AGENT_HUB_DATA_DIR` or `--data-dir` selects another directory. Each device configures its own accounts, paths and SSH aliases there.
- `config.example.json` is a template, not a snapshot of the developer's machine. Built-in CLI names can resolve through PATH; disabled integrations require setup before enabling.
- Tasks, SQLite databases, logs, worktrees and connection tokens belong to the selected runtime directory. Plugins receive a per-instance `context.data_dir` under `<data-dir>/plugins/<instance-name>` and must store mutable state there, never inside the installed package.
- Credentials stay in device environment variables or provider credential stores. `options` must contain only non-secret settings and references (such as an environment variable name), never secrets intended for distribution.
- Existing tasks contain absolute workspace/session references. Copying a database is **not** a supported cross-device task migration. Use the existing SSH node integration for execution on another device; workspace paths refer to that device. Source/configuration portability does not imply automatic artifact transfer or credential synchronization.
- Plugins are trusted executable code running with the Hub user's privileges. Contract validation is not process isolation. Review and install trusted packages explicitly. The Hub does not install packages or load every discovered plugin automatically.

## Install and verify on a new device

Use Python 3.10+ in a virtual environment. Activate that environment using your operating system's normal command, then run the same Python commands on Windows, macOS or Linux:

```sh
python -m pip install .
python -m pip install ./examples/echo-plugin
python -m agent_hub plugins
python -m agent_hub check-config
```

For a wheel distribution, replace `.` with the wheel filename. Install a plugin into **the same environment** as Hub. The `plugins` command lists entry-point metadata without importing plugin code. `check-config` loads explicitly enabled plugins and diagnoses executors locally without starting/stopping the shared daemon or sending model tasks. `available` does not establish valid provider authentication. A configuration error exits nonzero; a configured-but-unavailable executor is shown with `available: false`.

Merge the following into the private data directory's `config.json`:

```json
{"agents":{
  "writer":{"kind":"plugin","plugin":"echo","options":{"prefix":"Example: "}},
  "another-writer":{"kind":"plugin","plugin":"echo","options":{"prefix":"Second: "}}
}}
```

Start a new Hub or reload an existing daemon at a suitable idle time to apply configuration/package changes. Task calls use the configured instance name (`writer`), not the package or entry-point name. `list_agents` advertises supported modes and permission metadata. The echo package is a deterministic, account-free example; it does not call an AI model.

## Author an independently installed Python plugin

The example package contains a complete `pyproject.toml`, implementation and executable wrapper. Its registration uses standard Python distribution metadata:

```toml
[project.entry-points."agent_hub.executors"]
my-tool = "my_package:plugin"
```

The exported object is `ExecutorPlugin(api_version=1, create=factory)`. Hub calls `factory(config=options, context=PluginContext(...))`. The factory returns an executor implementing:

- `capabilities`: unique supported modes from `delegate`, `read_only`, `text_only`, `edit_worktree`, `continue`. Declare at least one submission mode. `continue` is only a capability; submit the original mode when continuing.
- `diagnose() -> dict`: contains boolean `available`; may include version and credential setup guidance, never credential values. Do not send model tasks during diagnosis.
- `permission_policy(mode) -> dict`: `enforced_by` (nonempty string), `enforced` (bool), `allows_write` and `allows_commands` (bool or null), `os_sandbox` (bool), optional human-readable scope. `read_only`/`text_only` require enforced no-write behavior; `text_only` also requires commands disabled. Declarations must describe actual integration behavior; Hub does not invent enforcement.
- `remote_cancel` (bool), `cancel_scope` (`local_process_tree`, `remote_session` or `remote_hub_task`). The SDK's defaults are conservative: cancellation is unconfirmed remotely. Never advertise guaranteed remote cancellation merely because the local request closed.
- `run(task, cancel, event) -> dict`: consume the supplied prompt, workspace, mode, deadline and optional `resume_session`/context; honor the cancellation event and deadline with bounded I/O. Return a JSON-serializable object containing string `answer` and optional nonempty string `session_id`, string-list `source_urls`, dict `artifacts`, list `media_artifacts`. Existing gateway artifact validation still applies.

For failures raise `HubError(code, safe_message, safe_details)`. `CANCELLED`, `TIMED_OUT`, `DELIVERY_UNKNOWN` and `REMOTE_STATE_UNKNOWN` preserve the corresponding Hub lifecycle. After uncertain delivery do not automatically resend. Do not put credentials in exceptions, diagnostics, answers or emitted events.

Use `event('progress', {'message': '...'})` for bounded progress. Advanced integrations can use existing Hub lifecycle events; those fields are trusted control data, not model output. Do not forward arbitrary remote/model JSON as Hub events. A remote session can be announced through `event('session', {'session_id': '...'})` once it is established so interruption recovery retains its identity. Use the task's prepared `workspace`, not the original cwd for editing. `edit_worktree` provides Git isolation, not an OS sandbox.

Instance names use lowercase ASCII letters, digits, `_` and `-`, start with a letter, and have at most 64 characters. Built-in names, the `hub-` prefix and Windows device names are reserved for cross-platform compatibility. Missing, duplicate or incompatible entry points fail explicitly; disabled entries are not imported. Different instances can load the same plugin with different options and state roots.

Python plugins can call APIs/MCP SDKs or wrap tool-specific CLIs. Provider dependencies belong in the plugin package, keeping Hub core independent. An in-process plugin must cooperate with deadlines/cancellation; for opaque or potentially blocking integrations prefer the command bridge, whose local process tree Hub can terminate.

## Command bridge for any implementation language

```json
{"agents":{"external-tool":{
  "kind":"command",
  "args":["-m","agent_hub_echo","{request_file}"],
  "resume_args":["-m","agent_hub_echo","{request_file}"],
  "modes":["delegate"],
  "result_format":"json"
}}}
```

Omitting `executable` uses Hub's Python interpreter. To invoke another language/tool set `executable` to a command on PATH or a device-specific executable path in the private config. There is no implicit shell; Windows `.cmd`/`.bat` launchers need an explicit appropriate executable or a native interpreter entry point. Wrappers can call any provider API or MCP client using their own dependencies.

`args` and optional `resume_args` are arrays, not shell command strings. Recognized placeholders are `{request_file}`, `{prompt}`, `{workspace}`, `{session_id}`, `{mode}`. Each value remains a single literal argument, including Unicode, whitespace and shell metacharacters; substitution is one-pass. Prefer the request file to keep prompts out of process command lines. Fixed arguments can contain JSON; bare named placeholders outside the supported set are rejected.

Hub writes a short-lived UTF-8 JSON file in the task's private log directory:

```json
{"schema":"agent-hub.command.v1","task_id":"...","prompt":"...","mode":"delegate",
 "workspace":"<prepared workspace on this device>","deadline":0,
 "resume_session":null,"context":null,"plugin_data_dir":"<private per-instance state directory>"}
```

`deadline` is the real Unix timestamp supplied at runtime. The file contains only protocol fields and is deleted when the process finishes, fails, is cancelled or times out. Logs and task records remain private runtime data under normal Hub retention behavior. A hard OS/process crash can leave a request file in that private log directory; do not package or share it.

With `result_format: "json"` (default), the final nonempty stdout line must be one object:

```json
{"answer":"result","session_id":"optional-native-session","source_urls":[]}
```

Only these response keys are accepted. Duplicate keys, wrong types and unknown fields fail. Log/progress text may precede the response. Native CLI session records and `hub_event` output are not interpreted. Exit nonzero on tool failure; do not report a success-shaped answer after a failed request. JSON output is required for `resume_args`. With `result_format: "text"`, stdout is the answer and continuation is unavailable. Existing log redaction applies to captured output.

`delegate` is the default; optional `edit_worktree` uses Hub's Git workspace preparation. The command adapter cannot claim read-only/text-only enforcement or an OS sandbox. On cancellation/timeout Hub stops the local process tree but reports `remote_cancelled: false`, because a provider may continue remotely. Remote uncertainty blocks automatic session reuse; a provider-specific plugin is needed to confirm remote cancellation.

Pass device credentials by environment **reference**, not plaintext config:

```json
{"env_from":{"PROVIDER_API_KEY":"MY_TOOL_API_KEY"}}
```

The source variable must exist in the daemon's environment. Values are resolved at execution time and are not returned by diagnostics. Restarting/reloading the daemon is needed if its inherited environment changes. `env_from` can also explicitly supply non-secret environment settings; unrelated environment variables are not automatically inherited.

## Independent content review

`acceptance.review.agent` may select any configured executor that supports `text_only`. The reviewer cannot be the task's executor. Missing/unavailable or unsuitable reviewers leave `needs_review`; malformed review output never becomes a passing result. Claude retains the legacy evidence identifier; other reviewers use `hub-content-review` with the configured agent recorded. The command bridge does not expose `text_only` and cannot be used for this by configuration alone.

## Verification scope

Contract tests cover discovery/version/config failures, disabled plugins, separate state roots, permissions, command requests/results, cancellation and continuation. Distribution tests build wheels/sdists, check for synthetic private files, install Hub plus the separate example package in a relocated directory, remove the build source and complete 12 tasks across two independent runtime roots. These are simulated device roots on one host, not proof of actual network or multiple-OS execution. The CI matrix runs the contract/distribution suite on Windows/macOS/Linux with Python 3.10 and 3.12 when CI is triggered. Real AI accounts, desktop automation and third-party plugins need their own acceptance tests.
