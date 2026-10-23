# Portable executor plugins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Connect new AI tools through separately installed plugins or command configuration without core edits.

**Architecture:** A versioned SDK validates explicitly selected entry points. A command adapter bridges external programs through a request file and structured response, retaining the Hub lifecycle. Runtime state stays outside distributed source.

**Tech Stack:** Python >=3.10, importlib.metadata, unittest, existing process runner and SQLite service.

**Spec:** `docs/superpowers/specs/2026-10-23-executor-plugins-design.md`

## Global Constraints

- Preserve built-in configuration and historical task semantics.
- No new required dependencies; Python >=3.10.
- No personal paths, credentials, runtime state or private evidence in tracked changes or distributions.
- Per-device private state; no claim that existing task records migrate across machines.
- No implicit permissions, shell interpolation, automatic retry or shared daemon restart.

## Review Focus

- Disabled or unrelated installed plugins must never execute during loading (Task 1).
- Duplicate names, invalid capability/permission/result declarations must fail explicitly (Task 1).
- Cancellation/timeout and malformed output must clean temporary request files and preserve uncertainty (Task 2).
- Same plugin used by multiple configured instances/data roots must not share state (Tasks 1, 3).
- Fresh installation must discover external plugins without source-checkout imports or host paths (Task 3).

### Task 1: Plugin contract and registration

**Files:** create `src/agent_hub/plugins.py`, `tests/test_plugins.py`; modify `src/agent_hub/config.py`, `src/agent_hub/service.py`.

**Interfaces:** `ExecutorPlugin(api_version, create)`, `PluginContext(name, data_dir)`, `Executor`; `load_plugin(name, item, root)` returns a validated adapter with diagnose/run/permission_policy/capabilities/remote_cancel/cancel_scope. Plugin factories receive `(config=options, context=context)`.

- [x] Write failing tests for selected-only loading, missing/duplicate/version errors, modes, invalid results, separate state roots and legacy config.
- [x] Run `python -m unittest discover -s tests -p test_plugins.py`; expect failures for absent SDK.
- [x] Implement SDK, entry point loading and configuration routing. Reject continuation where capability is absent, after idempotent replay handling.
- [x] Run the contract tests and relevant service tests; expect all pass.
- [x] Commit the plugin contract and registration.

### Task 2: Portable command adapter and reviewer routing

**Files:** create `src/agent_hub/adapters/command.py`, `tests/test_command_adapter.py`; modify `config.py`, `verification.py`, `review.py`, `service.py`, `mcp_server.py`, `tests/test_review.py`.

**Interfaces:** `Command(config, context)` implements the SDK executor contract. Config fields are executable, args, resume_args, result_format (text/json), modes (delegate/edit_worktree), env_from. The last nonblank stdout line for json must contain string answer and optional string session_id/source_urls; Hub internal state cannot be set by output.

- [x] Write and run failing tests for Unicode/space input, argument literals, cleanup, malformed/error output, timeout/cancel, env references and resume capability.
- [x] Implement command request/response handling using existing process-tree management; preserve conservative remote cancellation metadata.
- [x] Write and run a failing non-Claude review test and generalize reviewer selection while preserving legacy verifier identities and fail-closed behavior.
- [x] Run command, service and review tests; expect all pass.
- [x] Commit bridge and reviewer changes.

### Task 3: Distributable examples, docs and release validation

**Files:** create `examples/echo-plugin/`, `docs/executor-plugins.md`, `.github/workflows/plugin-contract.yml`, `tests/test_plugin_distribution.py`; update READMEs and portable example config.

**Interfaces:** example entry point `echo` exports `ExecutorPlugin`; `python -m agent_hub_echo REQUEST_FILE` implements command bridge v1; relocated install tests use public package APIs.

- [x] Write and run failing distribution/example tests before adding the example package.
- [x] Create deterministic external plugin/module and portable setup documentation; clarify actual AI adapters require provider-specific implementation/authentication.
- [x] Build Hub and example wheels, inspect archive contents, install into a separate directory and run a real isolated Hub lifecycle using discovered plugins and command bridge.
- [x] Run the full suite, source/diff privacy checks and packaging checks; record passed/skipped/unverified scope.
- [x] Commit examples/docs/validation, obtain independent whole-branch review, address material findings with regression tests, preserve branch/worktree for review.
