---
name: agent-hub-orchestrator
description: Orchestrate multi-agent tasks across Codex, Claude Code, Muse, Cursor, Grok Bot, and Antigravity using the AI-Integration-platform (HubGateway v1) MCP tools. Use when the user asks to dispatch tasks to local or remote agents, manage work items, switch executors, handle isolated Git worktree edits, recover unknown remote task states, or resume browser tasks after human intervention.
---
# Agent Hub Orchestrator

Coordinate heterogeneous AI executors (Codex, Claude Code, Muse, Cursor, Grok Bot, and Antigravity) through the HubGateway v1 MCP tools exposed by `local-agent-hub`.

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

Create a Work Item before every public submission, including single tasks: submit_task requires work_item_id.

- Call `create_work_item` with a unique request_id, a clear title, and optional acceptance criteria (put the goal in the task prompt) to obtain a stable `work_item_id`.
- After an attempt exists, call `claim_controller` with request_id, task_id, and your controller_id before controlled mutations or executor switches. Always pass the returned `controller_epoch` on subsequent controller operations to prevent split-brain writes.
- Use `link_task` if an earlier standalone `task_id` needs to be retroactively attached to the `work_item_id`.

### 3. Submit the Task with the Appropriate Isolation Mode

Choose the least-privileged `mode` that satisfies the task requirements when calling `submit_task` (include a unique request_id and the work_item_id; the MCP tool has no auto_start_worktree argument):

- Use `text_only` for pure reasoning or Q&A where file reads and shell commands are unnecessary.
- Use `read_only` for codebase exploration, architecture review, and bug diagnosis without modifying files.
- Use `edit_worktree` with a valid Git repository `cwd` for any code modification task. The daemon automatically creates an isolated `hub/<task_id>` Git worktree so concurrent edits never corrupt the user's working tree.
- Use `delegate` for browser automation (`muse`) or remote bot workflows (`grokbot`).
- Record key orchestration milestones on the shared timeline using `record_activity` so the Web Dashboard stays informative.

### 4. Wait for Completion and Stream Events Efficiently

Avoid tight polling loops with `get_task` because rapid polling wastes tokens and increases SQLite contention.

- Call `wait_for_task` (which performs a 0 to 10 second server-side long poll) until the task reaches a terminal state (`succeeded`, `failed`, `timed_out`, `cancelled`, `interrupted`, `delivery_unknown`) or pauses at `waiting_for_user`.
- If the task is still `running` after `wait_for_task` returns, optionally call `get_events` with the last seen `after_seq` cursor (up to `limit: 200`) to inspect incremental progress, then call `wait_for_task` again.
- Once the task reaches `succeeded`, call `get_result` to retrieve the final `answer`, `git diff`, `artifacts`, and `source_urls`.

### 5. Handle Human-in-the-Loop Browser Interventions (`waiting_for_user`)

When a browser executor like `muse` encounters a login wall, 2FA prompt, or CAPTCHA, `wait_for_task` returns `state: "waiting_for_user"` along with `needs_user: true` and an `intervention` object containing `intervention_id`.

- Stop polling immediately and present the prompt or screenshot artifact (retrieved via `get_artifact`) to the user.
- Wait for the user to confirm they have completed the action in the browser window.
- Call `resume_task` passing a unique request_id, the task_id and intervention.intervention_id, then resume monitoring with `wait_for_task`.

### 6. Continue Sessions, Switch Executors, or Verify Results

After an attempt finishes, decide whether to iterate in-place, hand off to another agent, or finalize verification:

- **Same-agent follow-up**: If the executor supports `continue` and returned a valid `session_id`, call `continue_task` to send follow-up instructions while preserving the native session context and worktree.
- **Cross-agent handoff**: If the current attempt failed or requires a different model (for example, handing off from `claude` architectural planning to `codex` implementation), call `switch_executor` with request_id, task_id, agent, mode, prompt and any controller lease fields to spawn a fresh attempt under the new agent while preserving work item history.
- **Verification**: For `edit_worktree` tasks, call `verify_task` to run the configured acceptance checks on a succeeded attempt; inspect the returned verification state and evidence before presenting the final result.
- **Remote state recovery**: If a remote executor task reports `REMOTE_STATE_UNKNOWN`, call `reconcile_task` before retrying so you never duplicate an unconfirmed remote submission.

## Gotchas

- **Never tight-loop `get_task`**: Always prefer `wait_for_task` for blocking waits. Use `get_task` only for non-blocking status snapshots across multiple tasks.
- **Never retry `REMOTE_STATE_UNKNOWN` blindly**: Remote executors (`grokbot`, remote sessions) may still be running. Always call `reconcile_task` first to probe remote state.
- **Respect `continue` capability boundaries**: `switch_executor` intentionally starts a clean native session on the target agent while inheriting Work Item context; do not pass an old agent's native `session_id` to a different executor adapter.
- **Clean cancellation**: When aborting a runaway task, call `cancel_task` and verify the state transitions to `cancelled` before starting a replacement attempt in the same directory.

## Current API details

- Use a stable unique request_id for idempotent mutations; reuse it only when retrying the same request.
- get_events returns next_seq; carry it forward. wait_for_task also returns has_more for pagination. wait_for_task waits for terminal state, human intervention, or timeout even if events arrive.
- Declare relation=review or relation=rework and related_task_id for those task roles under the same work_item_id.
- Executor success is distinct from acceptance passing; never infer a passed review from an ordinary chat response.
