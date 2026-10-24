# AI Integration Platform

**English** | [简体中文](README.zh-CN.md)

A local Agent Hub for coordinating Codex, Claude Code, Muse, Cursor CLI, and Grok Bot through one MCP interface. An independent daemon runs tasks, while SQLite stores their state, events, and results. Coding tasks can use isolated Git worktrees and return diffs for review. Collected edit_worktree diffs and untracked text excerpts share a 1 MiB UTF-8 budget (Workspaces.MAX_ARTIFACT_BYTES); over-budget payloads set artifact_truncated with artifact_omitted file reasons instead of returning a partial diff as complete. Path lists such as artifact_omitted and changed_files are metadata outside that byte budget; the whole result JSON is not capped.

**Python 3.10+ · Windows / macOS · Service version 0.3.0 · 16 MCP tools**

**Portable extensions:** [Executor plugins and command bridges](docs/executor-plugins.md) connect new AI tools without editing Hub core. Install plugins into the Hub environment; keep credentials, machine configuration and task state in each device's private data directory. Includes an independently installable example and Windows/macOS/Linux contract CI; platform CI configuration is not a claim of completed multi-platform validation.

## Features

- Persistent tasks, idempotent submission, session continuation, event cursors, and bounded waits.
- Executor-specific modes and permission descriptions, with isolated worktrees for editing tasks.
- Independent verification, artifact evidence, a local workbench, controller handoff, and opt-in content review through a configured text-only executor.
- Remote Hub access over standard SSH, optionally using an existing Tailscale network, without exposing the Hub on the public internet.
- A [read-only dashboard](docs/dashboard.md) with task search, recorded conversations, execution events, and separate verification status. Open it locally with `agent-hub dashboard`; packaged static assets need no Node.js runtime.

Implementation, historical live validation, and unverified behavior are documented separately. `ready=true` means execution has finished. For tasks with acceptance criteria, also inspect `verification` and `work_status`.

## Contents

- [macOS setup](#macos-setup) · [Windows setup](#windows-setup)
- [SSH and Tailscale](#ssh-and-tailscale) · [Connect an orchestrator](#connect-an-orchestrator)
- [Submit tasks](#submit-tasks) · [Executor limitations](#executor-limitations)
- [Diagnostics and validation](#diagnostics-and-validation) · [Task modes and permissions](#task-modes-and-permissions)
- [HubGateway](#hubgateway-v1-service-version-030) · [Muse human intervention](#muse-human-intervention)
- [Task verification](#independent-task-verification-phase-1) · [Workbench and controllers](#workbench-and-controllers-phase-2)
- [Nodes and artifact transfer](#nodes-and-artifact-transfer-phase-3) · [Historical validation](#historical-validation)

## macOS setup

Create an environment in the source directory with Python 3.10 or later. Python 3.12.14 and uv were used in the recorded macOS validation:

```zsh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
# The current tests explicitly import exceptiongroup.
uv pip install --python .venv/bin/python exceptiongroup
.venv/bin/python -m agent_hub status
.venv/bin/python -m agent_hub mcp
```

Configure executor paths in `~/.agent-hub/config.json`. For CLI executors (`codex`, `claude`, `cursor_cli`), `executable` supports environment variables, `~`, glob patterns, or a command resolved through `PATH`. Multiple glob matches use the newest modification time. For example, `%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe` accommodates changing installation directories. Paths are resolved for each diagnosis and launch; `status` includes `resolved_executable`. Absolute paths remain supported.

Muse, Remote, and Cursor IDE use their own path conventions rather than this CLI resolver. Muse requires a separate Python environment running `-m muse_mcp`; follow the [connector patch instructions](vendor/muse-mcp/README.md) before validating authentication. Register MCP with the absolute path to `.venv/bin/python` and arguments `["-m", "agent_hub", "mcp"]`.

Keep login data, cookies, and model keys in each executor's private user configuration. See the [macOS setup record](docs/2026-10-11-macos-setup-validation.md) for historical details.

## Windows setup

Use Python 3.10 or later and run from the source directory:

```powershell
& 'C:\path\to\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

Configure `%USERPROFILE%\.agent-hub\config.json` using [config.example.json](config.example.json). Replace sample paths and proxy placeholders. A network proxy such as `127.0.0.1:PORT` is separate from Claude's CCR model router on port 3456. Do not put cookies or model keys in Hub configuration.

See the [Windows executor validation record](docs/2026-10-12-windows-executor-validation.md). Cursor IDE needs its desktop executable and an online UI worker. Cursor CLI can use a native Node executable, entry script arguments, and a `credentials_file` referencing existing private BDK credentials. Its `sandbox` defaults to `enabled`. The recorded Windows CLI validation explicitly used a user-authorized `disabled` permission allowlist mode, which does not provide an OS sandbox.

Set concurrency with `{"concurrency":{"global":2,"per_agent":{"codex":1,"claude":1,"muse":1}}}`. Limits accept integers from 1 to 32; omitted per-executor limits default to 1. Tasks sharing the same actual workspace always run serially. Apply changes by stopping the daemon when no tasks are in flight; the next call loads the new configuration.

```powershell
.\.venv\Scripts\python.exe -m agent_hub status
.\.venv\Scripts\python.exe -m agent_hub mcp
# Explicit stop: local tasks are cancelled; remote work may continue.
.\.venv\Scripts\python.exe -m agent_hub stop
```

MCP uses stdio. Set `command` to the environment's absolute `python.exe` path and `args` to `["-m", "agent_hub", "mcp"]`. `AGENT_HUB_DATA_DIR` selects another private data directory. The default directory is protected for the current OS user; the Hub does not listen on external network interfaces. Windows automatic startup uses WMI so closing MCP does not terminate the daemon. If WMI is disabled by policy, start the `daemon` command in a terminal first.

## SSH and Tailscale

Machines on different networks can use an existing Tailscale network for SSH connectivity without a public IP or port forwarding. The Hub invokes the target's private Hub through local `ssh`; it does not install, sign in to, or manage Tailscale.

Recorded live validation covered Windows calling a Mac Hub, with Codex, Claude, Muse, Grok, and Cursor completing tasks. Transport fixes in `5ca9724` send JSON as UTF-8 bytes, invoke Python with `-X utf8`, and distinguish RPC errors from task failures. Unconfirmed submission/continuation is not automatically resent; any known target task ID is retained.

1. Install Tailscale on both machines and join the same tailnet or an appropriately shared network.
2. Confirm the peer is online with the client or `tailscale status`.
3. Configure an SSH alias using the peer's Tailscale IP or MagicDNS name. Keep port, key, and ProxyJump settings in SSH configuration.
4. Supply `--ssh-host`, `--remote-python`, and `--remote-data-dir`, or Remote fields `ssh_host`, `python`, and `data_dir`. Remote paths and task `cwd` refer to the target machine. Omitted new CLI connection options retain existing POSIX behavior.

Example SSH configuration for Windows calling a Mac; replace all placeholders:

```text
Host mac-hub
    HostName <Tailscale IP or MagicDNS name>
    User <Mac username>
    IdentityFile <path to the dedicated private key on Windows>
```

Connect manually with `ssh mac-hub` once to verify and accept the host fingerprint into `known_hosts`. The Hub uses `BatchMode=yes`, `StrictHostKeyChecking=yes`, and `ConnectTimeout=5`; unknown or changed host keys are rejected without prompting.

```powershell
.\.venv\Scripts\python.exe -m agent_hub status --ssh-host mac-hub --remote-python /path/to/project/.venv/bin/python --remote-data-dir /Users/your-name/.agent-hub
```

```json
{"agents":{"remote-worker":{"kind":"remote","platform":"darwin","ssh_host":"mac-hub","python":"/path/to/project/.venv/bin/python","data_dir":"/Users/your-name/.agent-hub","agent":"claude","cwd":"/Users/your-name/project","mode":"read_only"}}}
```

The target must accept the key and have the Hub and executor installed. Node declarations do not prove connectivity; `remote_connection_verified` remains `false`. Keys, `authorized_keys`, and configuration stay private. Connectivity depends on the user's tailnet and network environment. The cross-network live record covers Windows-to-macOS, not a live native Windows OpenSSH target.

### Native Windows SSH targets

For a Hub running under native Windows Python, pass `--remote-platform windows` and match the target OpenSSH default shell with `--remote-shell cmd` or `powershell`. Windows defaults to `cmd` if shell is omitted. The Hub does not probe/change DefaultShell or automatically fall back. Target `PATH` must resolve `powershell.exe` (Windows PowerShell 5.1); custom launchers, WSL, and Git Bash are outside this support.

```sh
python -m agent_hub status --ssh-host windows-hub --remote-platform windows --remote-shell cmd --remote-python 'C:/Hub/.venv/Scripts/python.exe' --remote-data-dir 'C:/Users/your-name/.agent-hub'
```

This uses POSIX shell quoting; use double quotes from Windows cmd. The same flags apply to `rpc`, `show`, and `mcp`. Send RPC arguments on stdin with `--arguments -`; prompts do not belong in the remote command.

```json
{"agents":{"windows-worker":{"kind":"remote","platform":"windows","shell":"cmd","ssh_host":"windows-hub","python":"C:/Hub/.venv/Scripts/python.exe","data_dir":"C:/Users/your-name/.agent-hub","agent":"claude","cwd":"D:/中文 项目","mode":"read_only"}}}
```

Drive-absolute and ordinary UNC paths are supported. Environment variables and `~` are not expanded. Drive-relative paths (`C:foo`), roots without a drive, control characters, device namespaces, ADS, and illegal filename characters are rejected. UNC access depends on the SSH user's permissions; the Hub does not provision network credentials. Unicode, spaces, quotes, `&`, `$`, backticks, `%`, `!`, and `^` are treated literally.

Both shells use a fixed `powershell.exe -NoProfile -NonInteractive -EncodedCommand` script encoded as UTF-16LE/Base64. PowerShell literals double ASCII and U+2018/U+2019/U+201A/U+201B single quotes according to [CodeGeneration.EscapeSingleQuotedStringContent](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.codegeneration.escapesinglequotedstringcontent).

Python starts through .NET `ProcessStartInfo` with `UseShellExecute=false` and `RedirectStandardInput=false`, inheriting SSH stdin. The script does not read stdin, reference `$input`, or create a text pipeline. stdout/stderr are forwarded independently as bytes through concurrent `BaseStream.CopyToAsync` calls. Forwarding-task return values are suppressed. The outer PowerShell launcher uses the same method to avoid turning native stderr warnings into error records that corrupt JSON. Python uses `-X utf8`; Windows CRT quoting preserves paths/trailing backslashes, and the child exit code is returned explicitly. Initialization/forwarding exceptions go to stderr and exit 1. Strict host-key checks, uncertainty handling, and the 8,000-character command limit remain in effect.

Source references include [.NET Framework Process](https://github.com/microsoft/referencesource/blob/main/System/services/monitoring/system/diagnosticts/Process.cs) and PowerShell 7.4 [ConsoleHost](https://github.com/PowerShell/PowerShell/blob/v7.4.0/src/Microsoft.PowerShell.ConsoleHost/host/msh/ConsoleHost.cs), [Executor](https://github.com/PowerShell/PowerShell/blob/v7.4.0/src/Microsoft.PowerShell.ConsoleHost/host/msh/Executor.cs), and [NativeCommandProcessor](https://github.com/PowerShell/PowerShell/blob/v7.4.0/src/System.Management.Automation/engine/NativeCommandProcessor.cs). Their input-pipeline behavior informed the direct .NET launcher; source inspection does not replace Windows PowerShell 5.1 target testing. The launcher does not use unsupported `-InputFormat None`; [5.1 documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1) lists Text/XML.

sshd still starts outer `cmd /c` or `powershell -c`. The Hub does not disable AutoRun/profile or change SSH configuration. Outer initialization must not write stdout, read stdin, or alter standard handles; inner `-NoProfile` cannot undo those effects.

Configured Remote nodes must explicitly declare `platform` as `darwin`, `linux`, or `windows`; `shell=posix` applies to macOS/Linux. Existing POSIX CLI and direct `SshClient`/Remote construction retain default `platform=posix` behavior.

Windows process tests use outer `cmd.exe /c` and `powershell.exe -c`, covering large UTF-8 stdin, smart quotes and injection paths, official escaping API comparison, separate stderr warnings, exit codes 0/37, and JSON integrity. They skip on macOS/Linux. These tests do not replace OpenSSH end-to-end validation; run `python -m unittest discover -s tests -p test_remote.py -v` in the target environment and separately validate actual SSH connectivity.

## Connect an orchestrator

Codex, Claude Code, and Cursor can connect to the same Hub as orchestrators. See the [three-agent setup record](docs/2026-10-11-local-three-agents.md). `cursor` with `kind=ide` selects the native IDE bridge; `cursor_cli` selects CLI execution. Example configuration disables Cursor IDE by default. Recorded IDE validation covers plain-text delegation and same-session continuation after Pro activation. An external UI-capable worker is required; no unattended resident driver is provided. IDE editing, cancellation, and session recovery after reopening remain unverified.

Register `agent_hub` in MCP settings with the absolute environment Python path and `["-m", "agent_hub", "mcp"]`. Reload MCP or open a new chat to discover the current 16 tools. Update registered paths when moving source or environments. The [Windows migration record](docs/2026-10-10-d-drive-migration.md) is historical; each device uses its own paths.

Example Claude Code configuration; substitute your own absolute Python path:

```json
{"mcpServers":{"agent_hub":{"command":"D:/path/to/AI-Integration-platform/.venv/Scripts/python.exe","args":["-m","agent_hub","mcp"],"env":{"PYTHONUTF8":"1"}}}}
```

Start with `claude --strict-mcp-config --mcp-config <absolute-path-to-config>`. The orchestrator must load Hub MCP; do not reuse the executor's empty MCP configuration or `--safe-mode`. Recorded Claude/DeepSeek API testing used `ENABLE_TOOL_SEARCH=false`, followed by explicit authorization for the needed Hub tools. Set it for the current terminal if needed, such as `$env:ENABLE_TOOL_SEARCH='false'`, and follow normal tool authorization without changing global permissions.

Example request: delegate research to Muse through `agent_hub` using `delegate` and the project's absolute directory, save the task ID, then retrieve `answer` and `source_urls` after `ready=true`. Check actual MCP calls and task results rather than relying on the model's description.

## Submit tasks

1. Call `list_agents` for configuration, versions, and modes.
2. Call `submit_task(request_id, agent, prompt, cwd, mode)` and save the returned `id`.
3. Call `wait_for_task(task_id, after_seq=0, timeout_seconds=10)`, or query `get_task` / `get_events` directly.
4. Read `get_result(task_id)` when `ready=true`.
5. Use `continue_task(request_id, task_id, prompt)` to reuse the native session and workspace; it returns a child task ID.

Use a stable, unique `request_id`. The same key/content executes once; changed content returns `REQUEST_CONFLICT`. Execution timeouts range from 10 to 7,200 seconds, defaulting to 1,800. Closing the orchestrator does not change the deadline.

### Waiting for tasks

`wait_for_task` is read-only, with a maximum/default per-call wait of 10 seconds; zero returns immediately. It returns early for `succeeded`, `failed`, `timed_out`, `cancelled`, `interrupted`, or `delivery_unknown`, and for Muse human intervention. MCP and authenticated loopback HTTP `/rpc` share its contract.

Responses include `state`, `events`, `next_seq`, `has_more`, and `timed_out`. `timed_out=true` means this wait expired; it does not cancel/resend the task. A task deadline instead produces `state="timed_out"` with the wait's `timed_out=false`.

Persist the last received `next_seq`. Continue with that cursor until terminal, draining remaining events while `has_more=true` before reading the final result. Pages contain at most 200 events and advance only through returned events. Resume from the last received cursor after disconnection and deduplicate by `seq`; do not resubmit a task because a wait response was lost.

The orchestrator continues the wait loop. The Hub does not wake finished chats or send desktop notifications. Shutdown returns `SERVICE_STOPPING`; reconnect with the same task/cursor. Updated code/tools require an idle, authorized daemon stop with `python -m agent_hub stop`; the next call starts the new code. Reload MCP for the current 16 tools. Earlier eight-tool live validation is historical.

Coding uses `edit_worktree` based on committed `HEAD`. Tracked files must be clean and a baseline commit must exist. Untracked files stay in the original directory, are excluded from the task worktree, and appear in `excluded_untracked_files`; `baseline` records the commit SHA. Deliberately prepare a baseline if uncommitted content is required. The Hub returns diffs, changed files, and log paths without automatically committing, merging, or deleting worktrees. Worktrees are not security sandboxes, and completion claims are not proof of passing tests.

## Executor limitations

- **Cursor CLI:** `read_only` uses ask mode; `edit_worktree` allows workspace file tools and native resume. Executor configuration is separate from orchestrator MCP; project MCP/hooks/permission configuration, force mode, and commands are not enabled. File, continuation, cancellation, and timeout probes were recorded; a full OS sandbox audit was not performed.
- **Grok Bot:** `delegate` through the session API published with Cursor BDK. Use a dedicated Bot; local `cwd` is not synchronized. Recorded validation covers continuation, cancellation, deadline interruption, and normal daemon exit. `remote_cancelled=true` requires independent stopped-turn confirmation, or natural completion followed by confirmed idle. Prior side effects are not rolled back. Uncertain dispatch/stopping retains private cross-data-directory locks; inspect the original session before resending. See [Cursor/Grok integration](docs/2026-10-11-cursor-grokbot-validation.md).
- **Codex:** `read_only` uses Codex's read-only sandbox; `edit_worktree` uses workspace-write. User MCP configuration is not loaded. Windows defaults to MXC, used in recorded Codex 0.162.0 probes. Configure `windows_sandbox` explicitly on other devices as appropriate; do not disable sandboxing to bypass permission issues.
- **Claude:** recorded authentication uses Windows CCR/apiKeyHelper and the macOS DeepSeek Anthropic-compatible API; subscription authentication is unverified. `text_only` has no tools; `read_only` allows Read/Glob/Grep; `edit_worktree` adds Edit/Write, without commands. Safe mode disables custom skills/plugins; empty strict MCP configuration prevents recursion.
- **Muse:** `delegate`, without Hub-enforced read-only access. Cloud and connected-machine writes depend on Muse/client permissions and approvals. Initial tasks use independent sessions. Session/message sequence/final-answer markers correlate replies without certifying a complete remote workflow. Cancellation/timeouts attempt original-session interruption and independently verify stopping; inspect `remote_cancelled`. Unconfirmed remote work may continue. Uncertain sends are not automatically retried.

Muse merges message links and later `external_link` attachments into `source_urls`, returning after two stable polls. These sources are not independently verified pages. Inner MCP `ok=false` is a failure with upstream codes retained. Only transient history-read gateway, timeout, or VM errors retry, up to three attempts with 1-/2-second backoff, bounded by cancellation/deadline. Authentication failures, malformed responses, session creation, and sends do not automatically retry.

`completion_evidence` retains `reply_to_message_id`, possibly empty; a nonempty value does not exclude a completed task-marked reply. Exhausted reads preserve remote uncertainty. Sends use `fire_and_forget` for dispatch confirmation; history polling collects replies. Dedicated workers enable `MUSE_MCP_REUSE_GATEWAY=1` for locked connection reuse. Errors discard the cache without replaying business operations; ordinary Muse tools retain separate connections. Apply the [connector patch](vendor/muse-mcp/README.md) and configure your own authentication/Python path.

Daemon recovery marks `running`/`cancelling` as `interrupted`, without killing old PIDs or rerunning tasks. Workspaces with uncertain old execution are quarantined; direct continuation is rejected and already queued tasks in that workspace also become interrupted. Other workspaces' queues continue. Inspect old execution and submit into a new workspace as appropriate. Normal terminal results can reuse saved `session_id`. Force-killing a macOS daemon does not guarantee its old executors stop.

See historical macOS [core](docs/2026-10-11-macos-full-acceptance.md) and [resilience](docs/2026-10-11-macos-resilience-validation.md) records. Strict physical-sleep deadline handling, DeepSeek Harness, and Claude subscription authentication were not validated there. macOS installs with `pip install -e .` in its own environment, without the Windows lockfile.

## Diagnostics and validation

`status` checks paths, versions, and configured capabilities; actual tasks establish account validity. For failures, read `get_result` and private logs. For `delivery_unknown`, inspect the original Muse session before sending again.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# Explicit authorization required: this uses real accounts and quota.
.\.venv\Scripts\python.exe scripts/acceptance.py
```

Designs, plans, progress, and validation records live in [docs](docs/). Authentication/runtime data are excluded from Git and full logs stay private. Most linked detailed records are in Chinese. Historical macOS validation did not cover physical-sleep recovery, independent auditing of Muse browser actions, hours-long stability, or natural login expiry recovery. Remote cancellation and SSH scheduling were implemented later, with cross-network Windows-to-Mac validation recorded on 2026-10-13.

`scripts/macos_*.py` use private validation directories and request keys; replaying saved results is not new validation. Real-account scripts consume quota. Sleep scripts suspend the whole Mac and require explicit authorization and a manual wake-up plan. They are outside ordinary automated regression.

## Task modes and permissions

| Executor | Mode | Enforcement |
|---|---|---|
| Claude | `text_only` | Text analysis without tools |
| Claude | `read_only` | Read/Glob/Grep; no writes, commands, or MCP tools |
| Claude | `edit_worktree` | Isolated worktree; Read/Glob/Grep/Edit/Write; no commands or OS sandbox |
| Codex | `read_only` | Codex read-only sandbox |
| Codex | `edit_worktree` | Codex workspace-write sandbox and isolated worktree |
| Muse | `delegate` | Muse/client permissions; no Hub-enforced read-only guarantee |
| Cursor CLI | `read_only` / `edit_worktree` | Ask or workspace-file allowlist; no Shell/MCP/WebFetch; OS sandbox not comprehensively audited |
| Grok Bot | `delegate` | Dedicated cloud Bot; remote interruption attempted and independently checked; no Hub-enforced read-only guarantee |

`list_agents` describes `enforced_by`, `enforced`, `allows_write`, `allows_commands`, `os_sandbox`, and scope per mode. `submit_task`, `get_task`, and `get_result` also return `permissions`. `enforced` refers to the stated executor restriction, not equivalent isolation across executors. Muse write/command flags describe possible actions, not client authorization.

Historical records and request idempotency are retained. Old Claude `read_only` tasks continue as `text_only`; old Muse `read_only` tasks cannot continue and require a newly scoped `delegate` task. Successful execution still requires checking files or external results. Reload the idle daemon and refresh client MCP tool definitions after updates.

## HubGateway v1 (service version 0.3.0)

`get_capabilities()` and `get_artifact(artifact_id)` expose capabilities and registered artifacts. The initial Gateway stage had 11 MCP tools; the current service includes later verification features for a total of 16. Optional `submit_task.context` participates in the idempotency hash in full; omission or `null` retains compatibility with earlier requests. Original task `state` is retained alongside `gateway_state`, `execution_uncertain`, `errors`, and `media_artifacts`. See the [Gateway handoff](docs/HubGateway-v1-实现交接.md).

## Muse human intervention

Muse may report nonterminal `waiting_for_user` for human verification, login, or approval. `get_task`, `get_result`, and `wait_for_task` expose an `intervention` containing its ID, reason, message, Muse session ID, and deadline; `ready` stays `false`. A wait returns early with `needs_user=true`, `timed_out=false`. Stop polling and ask the user to handle the original Muse session.

Only after explicit user confirmation, call `resume_task(request_id, task_id, intervention_id)`. It resumes the original task/session without a child task; each intervention delivers at most one continuation even with different request IDs. Query the original task afterward. Do not resubmit or call `continue_task` on the waiting task. `acknowledged=true` confirms local registration, not a verified final answer.

Each task has a total human-wait budget of 15 minutes. Normal execution time is paused during intervention and resumes with its remaining budget. Human-wait expiry, cancellation, and service stop use original-session interruption with independent stop verification. Uncertain stopping retains the lock without automatically resending. If Muse finishes after the user acts in the browser, the original task result can also be collected.

Detection prioritizes the prompted `[HUB_WAIT:<task_id>:human_verification|login|approval]` marker, with conservative detection of explicit blockers in the latest assistant message and recorded `evidence`. Native structured intervention fields are not verified. This is not general browser-state detection; manual inspection may be necessary when the model does not report a blocker. The Hub does not perform human verification.

The current MCP surface has 16 tools. Reload an idle daemon and reconnect clients to discover `resume_task` and updated wait definitions; old running workers are not hot-upgraded. SSH intervention passthrough was outside the recorded implementation stage. Gateway media validation covers `text.review/deepseek-flash[1m]` and `image.generate/muse:auto`; the latter is a default route with an unknown underlying model and covers one PNG without a reference image.

## Independent task verification (phase 1)

`submit_task` accepts an `acceptance` object; paths are relative to the actual task workspace:

```json
{"criteria":[{"id":"output","kind":"file_exists","path":"output.txt"},{"id":"digest","kind":"sha256","path":"output.txt"}]}
```

Additional kinds are `artifact_required` and `content_review`; the latter defaults to human judgment unless Claude automatic review is explicitly enabled below. `required` defaults to `true`. Acceptance criteria participate in request idempotency and are inherited by `continue_task`.

Execution state and the Gateway contract remain distinct from `verification` and `work_status`: verification `passed` maps to `completed`, while successful legacy tasks without criteria retain `succeeded`. `ready=true` only means execution ended. `get_work_item(task_id)` summarizes attempts, check reasons, artifact IDs/hashes, and pending actions. `verify_task(request_id, task_id)` idempotently reruns verification.

```sh
python -m agent_hub show TASK_ID --data-dir /path/to/private-data
# Human judgment is available only through authenticated local RPC, not MCP.
python -m agent_hub rpc record_verification --data-dir /path/to/private-data --arguments '{"request_id":"review-1","task_id":"TASK_ID","verifier_id":"human","decision":"pass","reason":"Manually checked"}'
```

MediaFlow submits `context` plus `acceptance`, reads `get_work_item` / `get_result.verification`, and writes external checks through `record_external_verification`. The Hub does not generate images or decode video, and this external-verification integration has not been connected to Muse. See the [verification design](docs/2026-10-12-verification-phase1-design.md). Reload clients to discover the current 16-tool surface.

Acceptance attempt chains are linear. Only the latest attempt can continue; new requests against an older one return `ATTEMPT_SUPERSEDED`, while same-key replay remains idempotent. `artifact_required` requires a new file or changed content hash; preexisting declared files, touch-only changes, or another task's media are not new evidence. Verification exceptions retry within bounds, then become `needs_review`.

`show TASK_ID --ssh-host HOST --remote-python /path/to/python --remote-data-dir /path/to/data` reads remote handoff data through SSH's read-only `get_work_item`. `verify_task` and `record_verification` are not exposed over SSH; human judgments use authenticated local RPC.

Configure verification scan/shutdown budgets in the data directory's `config.json`; defaults are:

```json
{"verification":{"max_files":1000,"max_bytes":268435456,"max_entries":5000,"max_seconds":10,"join_seconds":20}}
```

Only in-range integers are accepted; unknown fields return `INVALID_CONFIG`. See the design for ranges. Registered media do not consume the file-read budget. Incomplete baselines go to human review. Identical regenerated content may be classified as `copied` and does not by itself satisfy `artifact_required`.

## Workbench and controllers (phase 2)

Codex, Claude, and Cursor CLI map structured output to execution events. Missing tool/approval information stays explicit; narrated progress is not verification evidence. Initial phase-2 inspection covered CLI help only. Later recorded automatic-review chains exercised Cursor CLI and Claude structured output, without validating every tool/approval event type.

The local workbench is `http://127.0.0.1:PORT/work/TASK_ID`. A trusted local client first sends `Authorization: Bearer TOKEN` to `POST /work/TASK_ID/session`, then supplies the returned task-bound HttpOnly cookie to the same browser session. Page and `/events?after_seq=N` requests use that cookie; other task paths return 401. Validated `attempt_id` ownership allows reading the same attempt chain without widening cookie scope. Bearer authentication can also retrieve the page. Do not put tokens in URLs, HTML, or command arguments. `show TASK_ID --data-dir DATA_DIR` provides a text view.

The workbench distinguishes aggregate `work_status`, current attempt, execution state, and verification state. `succeeded` is not acceptance. Each attempt shows its execution timeline, criteria, current decision/reasons, and deduplicated artifacts; new decisions replace old ones.

Two channels synchronize the page:

- **Events:** `/events?attempt_id=...&after_seq=N` long-polls for up to 10 seconds. Current/nonterminal attempts poll continuously; old terminal attempts poll every 30 seconds for later review or continuation events. State events update execution status immediately.
- **Snapshots:** authenticated `GET /work/TASK_ID/snapshot` returns `Cache-Control: no-store` and current attempt/work/verification state, pending actions, and per-attempt `task_id`, `parent_id`, `attempt`, execution state, work status, verification, and artifacts. Events, sessions, workspace paths, and controller leases are omitted. Snapshots govern top-level status. Relevant events trigger refreshes, with fallback polling every 3 seconds while work/verification is active and every 15 seconds after completion. A new attempt triggers a page reload.

Failures are tracked separately for each channel. Network/non-2xx failures show the interrupted channel, reason, count, and last snapshot time, then retry at 1, 2, 4… seconds, capped at 30. A 401 prompts renewed session authorization and retries every 5 seconds. Snapshot recovery requires a successful snapshot; event recovery requires successful polling for each affected attempt. Only both healthy channels indicate live synchronization.

Claim control through MCP or local RPC; SSH also supports claims and `get_work_item`:

```json
{"tool":"claim_controller","arguments":{"request_id":"claim-1","task_id":"TASK_ID","controller_id":"controller-A"}}
```

Use the returned `controller_epoch` with `controller_id` on subsequent `continue_task`, `cancel_task`, `verify_task`, `record_verification`, `record_external_verification`, and `switch_executor` calls. A later claim increments the epoch and old controllers receive `CONTROLLER_STALE`. Same-key claims replay the original receipt. Legacy tasks without leases remain directly operable.

`switch_executor` takes `request_id`, `task_id`, `agent`, `mode`, `prompt`, plus optional lease fields. It hands off a terminal current attempt into a new attempt carrying goals, criteria, failure reasons, artifact hashes, and recent events, without reusing the old native session. Use `continue_task` for same-executor continuation.

Configure bounded automatic rework in the data directory:

```json
{"verification":{"max_auto_rework":2}}
```

The range is 0–5; zero disables it. A definite verification failure with no inconclusive checks may continue the original executor/session up to this shared limit. All attempts are retained and the same verification run is not dispatched twice. Restart recovers verification-completed/rework-not-dispatched windows using the same run/budget without verifying again. `needs_review`, unavailable verifiers, and manual/external review failures without `acceptance.review` do not trigger model rework. With review enabled, only Hub-validated configured-reviewer failures do. A `copied` file can satisfy artifact requirements only with an explicit expected SHA-256 for the same path matching actual content.

Review writes remain local-RPC-only. A `record_verification` content-review result needs a reason and registered artifact evidence. Without explicit automatic review, missing content judgments remain `inconclusive` / `needs_review`; no review model is called.

### Opt-in Claude content review

Add `review` to a task's acceptance object:

```json
{"criteria":[{"id":"out","kind":"file_exists","path":"report.md"},
             {"id":"quality","kind":"content_review","description":"The report covers three conclusions and supporting evidence"}],
 "review":{"agent":"claude","timeout_seconds":600}}
```

`agent` selects a configured executor supporting `text_only` (including a compatible plugin); timeout is an integer from 30 to 3,600 seconds, default 600, and at least one content-review criterion is required. An executor cannot review itself. Requests without `review`, historical tasks, and their hashes keep their existing behavior; there is no bulk background model dispatch. The Claude example above remains compatible; command bridges do not advertise `text_only`.

After deterministic checks pass, verification/work status becomes `reviewing`. An internal `auto-review:<review_run_id>` request creates an **independent root task** using the configured reviewer in `text_only` mode, no tools, and an empty private cwd. It is not a child execution attempt and does not change `current_task_id`. Normal global/per-executor concurrency applies. The review ID appears in `verification.review.task_id`, review events, and the workbench and can be queried independently. Failed deterministic checks defer model review and use ordinary rework rules.

Evidence is limited to registered workspace text artifacts from the current verification: at most 10 files, 32 KiB each, 64 KiB total, strict UTF-8, no symlink traversal or workspace escape, and SHA-256 rechecked at read time. Random boundaries mark content as untrusted. Registered media, remote workspaces, binary/oversized/changed evidence produce `needs_review` with a reason without calling the model.

The configured reviewer must return one plain JSON object. IDs below are illustrative and must exactly match the actual review context:

```json
{"schema":"agent-hub.content-review.v1","task_id":"d0d1eb9b8238495995fe4f2c17d01332","review_run_id":"4381dc4da64b419ab58145a6195140dc","verification_run_id":"0ee7778bdb2a4df0bb6418eb7197e956","checks":[{"criterion_id":"quality","outcome":"fail","reason":"Output must contain only GOOD followed by a single LF","evidence":["2eb0a44e321d5e1c81dc007971dd516d"]}]}
```

`outcome` must be `pass`, `fail`, or `inconclusive`; every content-review criterion appears exactly once, reasons are nonempty, and evidence references only supplied artifact IDs. Code fences, prose, tool-call/DSML text, missing/duplicate/unknown criteria, mismatched fingerprints, forged evidence, CLI failure, timeout, cancellation, or unavailable review configuration fail closed to `needs_review`, retaining the invalid/unavailable reason without retries or rework. One historical run produced DSML Bash-call text; no command ran because `text_only` had no tools. Stronger prompts do not guarantee valid output.

Before adopting the decision, the Hub rereads evidence and checks SHA-256. Changed/deleted/symlinked/escaped files become `review.state=evidence_changed` and `needs_review`, without applying the old decision or reworking. A changed controller lease before dispatch supersedes review without a model call. Cancelling a `reviewing` execution attempt stops its review task; the response reports `review.review_state` as `stopping`, `stopped`, or `not_dispatched`. Verification becomes `needs_review`, execution stays `succeeded`, and automatic rework does not run.

All passes produce `passed` / `completed`. Any required failure produces `rework_required`, using the shared `max_auto_rework` budget to continue the same executor/session with inherited acceptance and fresh checks. Optional failures are recorded without blocking or independently triggering rework. At the limit, `pending_actions` records the cap. Inconclusive decisions produce `needs_review`.

Results and an `auto-review-result:<run>` receipt are committed in one internal transaction. Results arriving after re-verification, human/external judgment, a new attempt, or a controller epoch change are recorded as superseded history; a still-reviewing attempt falls back to `needs_review`. Human judgment or re-verification can intervene during review. `auto-review` request prefixes and `hub-` verifier IDs are reserved. Restart marks in-progress review interrupted/needs-review; completed results are applied once through receipts without resubmitting the model.

External projects can report completed media checks against existing criteria through local `record_external_verification`:

```json
{"request_id":"media-check-1","task_id":"TASK_ID","verifier_id":"media-project-checker","controller_id":"orchestrator","controller_epoch":1,"checks":[{"criterion_id":"dimensions","outcome":"pass","reason":"The external project verified the required dimensions","evidence":["REGISTERED_ARTIFACT_ID"]}]}
```

Send JSON on stdin with `python -m agent_hub rpc record_external_verification --data-dir DATA_DIR --arguments -`. Evidence must belong to the attempt and criterion IDs must already exist; external size/quality checks can be declared as content review. Requests are idempotent and late old-attempt results become superseded history, even for execution failures. Review/external-result writes are not exposed over MCP/SSH. The Hub does not generate images/decode video; Muse integration and real Windows external-check validation remain unverified.

CLI events and native session IDs come only from stdout; stderr is redacted into logs. Only the first initialization record sets the session ID; later init/final records cannot replace it. Missing valid initialization disables native continuation. Permission-denial logs retain only tool names and call IDs. Superseded external results still require valid criterion/evidence ownership.

## Nodes and artifact transfer (phase 3)

`list_nodes` exposes node declarations through local RPC, MCP, and SSH. Remote configuration explicitly declares `platform` (`darwin`, `windows`, `linux`) and may declare `node_id` / `host_id`. A nonlocal node cannot reuse the local host ID; platform is recorded from the local system, including Linux as `linux`. Configuration alone does not prove connectivity; `remote_connection_verified` remains false.

Optional `submit_task.node_id` selects a declared node supporting the requested executor/mode, otherwise `NODE_UNAVAILABLE`; omission preserves existing routing. SSH nodes use the Remote adapter's `delegate` mode and do not upload local cwd. See [SSH and Tailscale](#ssh-and-tailscale) for network configuration.

Cross-host `get_artifact` accepts `target_host_id` or derives it from `target_node_id`. Different hosts receive `transfer_required` without sender paths. Authenticated local Bearer RPC alone exposes transfer:

```json
{"request_id":"send-1","artifact_id":"artifact://REGISTERED_ID","target_node_id":"TARGET_NODE"}
```

The receiver checks the hash and writes a receipt. Same-request replay does not recopy; restart/timeout retains `retryable`, without changing execution/verification state. SSH transfer returns `NODE_UNVERIFIED`; other-host local receiving currently exists only as injected two-directory test simulation, not production cross-machine transfer. Replace example controller fields with a real lease, or omit them for an unleased task.

For SSH/Remote tasks, sender-side `file_exists`, `sha256`, and `artifact_required` are always `inconclusive` because remote workspaces are outside local verification scope. They do not read local cwd as evidence or automatically rework because a remote artifact is missing locally. Run checks in the target Hub's workspace; remote success narration is not evidence.

Receipts use `(sender_host_id, request_id)` and filenames containing a sender-host hash, while stored content is reused by SHA-256. Equal request keys from different senders cannot collide and new request keys do not duplicate identical content. Configure `artifact_transfer` in `config.json`:

```json
{"artifact_transfer":{"max_received_bytes":1073741824,"max_received_files":10000}}
```

Defaults limit storage to 1 GiB and 10,000 files/receipts. Exceeding quota returns `ARTIFACT_QUOTA_EXCEEDED` without a success receipt. Old receipts migrate as `legacy-unattributed` history, never replaying as known senders; verifiable old files enter the content index. Idempotent replay precedes node-capability checks so a previously completed same-key request replays even when the node later becomes unavailable.

## Historical validation

These records describe specific dates, platforms, and source baselines. Editing these READMEs does not rerun any recorded live validation; current behavior/tool counts follow the operational sections and source.

- **2026-10-13:** Windows and macOS on separate networks completed Windows-to-Mac Hub tasks through Tailscale and standard SSH for Codex, Claude, Muse, Grok, and Cursor. SSH transport fixes are in `5ca9724`. The Hub does not manage Tailscale or listen publicly.
- **2026-10-12:** opt-in Claude content review was integrated after baseline `16110f2`. Verification phases 1–3 correspond to `08f0542`, `b495f73`, and `807ed0c`. The recorded macOS full run covered 287 tests, with one Windows ACL skip; 28 tests targeted automatic review. Human judgment, external verification writes, and transfer remain authenticated local-RPC operations outside the MCP surface.
- **Recorded live review chains:** Cursor CLI execution plus Claude CLI review covered direct pass, fail/rework/pass, and stopping at `max_auto_rework=1`, without orchestrator continuation or manual verification writes. Headless Chrome matched workbench/API state. Private evidence is under `~/.agent-hub/auto-review-20261012/real-chain-summary.json` (direct pass: run-173812; rework/cap: run-174333). Earlier failures remain preserved: run-172722 hit a registration TypeError; a run-173812 rework chain hit ECONNRESET and was rerun. These results validate those chains and fail-closed handling, not universal model output correctness.
- **Windows validation scope:** the [Windows record](docs/2026-10-12-windows-executor-validation.md) covers core regression and greetings through six entry points; its 259-test baseline predates automatic review integration. Windows live automatic-review chains, Muse verification writeback, and a non-headless GUI workbench were not covered there. The later cross-network test does not validate those features.
- **2026-10-11:** Windows v1/macOS integration and bidirectional Cursor CLI/Grok Bot tasks/continuation were recorded in the [integration report](docs/2026-10-11-cursor-grokbot-validation.md). The 129-test run had 128 passes and one Windows ACL skip (49.332 seconds), with 11 MCP tools at that stage. Windows entries from 10-09/10-10 were later supplemented by the 10-12 record. [Progress](docs/开发进度.md) and [v1 acceptance](docs/第一版验收.md) retain historical counts, not the current tool count.
- **Muse cancellation and SSH scheduling:** original-session cancellation, deadline interruption, and normal daemon exit were exercised in a dedicated session. The 10-11 SSH test used two isolated Hubs; actual cross-network validation followed on 10-13. See the [implementation record](docs/2026-10-11-muse-cancel-remote-hub.md) and [verification design](docs/2026-10-12-verification-phase1-design.md). Strict physical-sleep deadline recovery remains outside the recorded validation.
