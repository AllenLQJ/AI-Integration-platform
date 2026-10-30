# Runtime hardening implementation plan

> **For agentic workers:** Use superpowers:executing-plans; track each task below.

**Goal:** Apply the supplied six-task bundle to the current repository without losing newer functionality.
**Architecture:** Preserve pooled SQLite connections and suspended Windows Job creation. Explicitly serialize write transactions; notify long pollers after commit through a generation-counted Condition. Bound HTTP workers and validate plugin configuration before loading code.
**Tech Stack:** Python >=3.10, sqlite3, threading, pywin32, pytest.
**Spec:** `../specs/runtime-hardening-CODEX_TASK.md` and `../specs/runtime-hardening-reference.md` (supplied bundle; examples adapted to actual interfaces).

## Global constraints
- Keep current public APIs and TIMED_OUT/cancelled state contracts.
- No automatic worktree deletion, deployment, Hub restart, merge or push.
- mcp>=1.30.0,<2.0.0; httpx>=0.28.1,<1.0.0; retain Pillow and Windows pywin32.
- Retain untracked files in the main checkout.

## Review focus
- Pooled and nested connections: commit/rollback, all writer call sites, no read/write lock inversion.
- Events committed between snapshot and wait, shutdown wakeup and externally committed updates.
- Saturated HTTP worker capacity and thread-start failure must release sockets/permits.
- Parent group signaling and Windows handle cleanup on failed spawn/bind.
- Empty/exact-match allowlists and symlink/junction escapes before plugin import.

## Execution
- [x] Prerequisite: reproduce and fix Windows Antigravity import/getuid/PATH portability; rerun baseline.
- [x] Task 1: add orchestrator skills at requested paths and packaged src path; correct stale MCP fields using mcp_server.py.
- [x] Task 2: apply dependency ranges/dev extra and package skill; build/inspect wheel.
- [x] Task 3: tests for per-connection PRAGMAs, rollback, concurrent writes with zero busy timeout; add RLock/write=True to mutation contexts while retaining read concurrency.
- [x] Task 4: mocked POSIX own-group and escalation regressions plus existing real subprocess tests; preserve Windows suspended job assignment, add bind helper with cleanup.
- [x] Task 5: worker saturation/start failure tests; post-commit notification and missed-wakeup/idle-query tests; wire both polling endpoints and shutdown.
- [x] Task 6: denial-before-load, explicit/environment lists and escaping link tests; minimal plugin changes.
- [x] Verification: targeted red/green tests, full pytest, py_compile, wheel content, diff --check and independent final review.

## Decisions and evidence
- Ruling: adapt absent run_command/ProcessRunner APIs to spawn_process/run_process/terminate_tree; preserve TIMED_OUT and stronger Windows isolation. Literal replacement would break existing adapters.
- Ruling: skill examples follow current MCP signatures, mandatory work_item_id and actual terminal states. Bundle text is stale; exact copying would give users invalid calls.
- Ruling: update write=True call sites outside Store because otherwise other modules bypass serialization.
- Ruling: fix Antigravity portability as prerequisite: baseline collection fails with ModuleNotFoundError: pwd on Windows.

## Validation ledger
- Initial Windows baseline: collection failed in Antigravity on import pwd; existing tests reproduced the failure before the portability fix.
- Initial targeted regressions: 11 failures, then missing bind helper reproduced by 4 failure-path cases.
- Targeted after fixes: 20 passed, 2 platform skips (runtime hardening plus real Windows process tests).
- All 42 source Python modules compiled successfully.
- Wheel built with source-equivalent packaged skill and the required dependency metadata.
- Independent read-only review: no actionable new defects identified; live POSIX behavior and extended stress were not claimed as verified.
- Test environment: isolated worktree .venv, system site packages plus locally installed project dev dependencies. Main checkout's venv unchanged.
- Ruling: close excess HTTP connections immediately instead of blocking the accept loop for five seconds; this preserves responsiveness at capacity. Clients can retry rejected connections.
- Ruling: clamp workbench page size to 1–200 because the existing event API caps responses at 200; oversized configured values therefore do not enlarge the protocol limit.
- First complete run: 582 passed, 1 failed, 6 skipped. The failing pre-existing connection-counting wrapper rejected write=True. Adapted the wrapper to preserve/forward the new optional parameter; its six scheduler tests passed without weakening assertions.
- pip check: no broken requirements found.
- Final full run: `.venv/Scripts/python.exe -m pytest tests -q -rs --disable-warnings --tb=short` -> **583 passed, 6 skipped, 0 failures**, 303.59 seconds.
- Skips: two POSIX process tests, one POSIX SSH fake-executor test, three Unix file-descriptor browser tests. These are not claimed as runtime passes on Windows.
- Final state: reviewable uncommitted changes retained in the managed worktree; no merge, push, daily Hub restart, or automatic worktree removal.
