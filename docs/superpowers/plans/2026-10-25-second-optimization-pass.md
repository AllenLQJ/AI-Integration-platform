# Second optimization pass implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement task by task.

**Goal:** Implement the five verified fixes approved in the current conversation.

**Architecture:** Preserve the existing Service/SQLite and dashboard interfaces. Mark uncertain workspaces before publishing terminal state; degrade unreadable results per node; bound browser event windows and expose older pages; batch verification projections.

**Tech Stack:** Python 3.10+, SQLite, browser ES modules, unittest, Node test runner.

**Spec:** The five findings and proposed remedies in this conversation, explicitly approved by the user with “按照你说的来”. Continue inline on the existing feature branch, preserving the earlier uncommitted changes.

## Global constraints

- No provider calls, daily Hub restart, push, or deployment are needed for these fixes.
- Keep execution/verification truth intact when a result file is unavailable.
- Retain at most 1,000 events and approximately 2 MiB of serialized event data in the browser; old events remain available through bounded pages.
- Preserve existing work-item ordering, leaf selection, controller leases, and acceptance semantics.

## Review focus

- Unknown remote state must block queued and newly submitted work sharing the workspace; confirmed cancellation must remain usable.
- Evidence-only changes must redraw details even if task summaries do not change.
- Missing, malformed, or non-object JSON results must not hide other chain members.
- Event window eviction must also remove DOM entries; historical navigation must survive polling and node changes.
- Batched work-item reads must preserve branching histories and bound per-task recent events.

### Task 1: Workspace isolation
- [x] Add failing tests for unknown remote state, queued follow-up, confirmed stop, and postprocessing fallback.
- [x] Mark affected workspaces before terminal publication and slot release; verify related suites.

### Task 2: Verification evidence refresh
- [x] Add a real-app DOM regression for checks-only updates.
- [x] Include all displayed detail fields in refresh detection; run frontend tests.

### Task 3: Result-file resilience
- [x] Add failures for missing/corrupt/non-object files in standalone and chain views.
- [x] Share a safe result reader and surface RESULT_UNAVAILABLE per node without inventing execution failure; run dashboard/result tests.

### Task 4: Bounded event history
- [x] Add failures for event count/byte budgets, DOM eviction, cursor continuity, and historical paging.
- [x] Add before_seq paging through Store, Dashboard, HTTP and browser API; add earlier/latest controls and bounded state windows.
- [x] Verify HTTP authentication/query validation and frontend interaction.

### Task 5: Batched verification aggregation
- [x] Add query-budget and branching-history regressions.
- [x] Traverse in-memory task indexes and batch checks, artifacts, verification rows, and indexed recent-event queries.
- [x] Run verification/review/reconcile regressions, full Python/frontend suites, wheel build and diff checks.

### Completion
- [x] Perform one independent review and address material findings with failing regressions.
- [x] Record validation and remaining limitations, leaving changes reviewable in the current branch.
