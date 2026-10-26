# Output and scheduler hardening implementation plan

**Goal:** Implement the six accepted Cursor findings in order, then obtain a fresh Cursor review and perform a local review.

**Scope:** Continue on the existing feature branch. Preserve earlier uncommitted changes and unrelated files. Do not restart the daily Hub, push, or deploy. Keep command/remote cancellation conservative.

1. [x] Add real child-process regressions for pump callback/logging failures; propagate failure to the controlling thread, stop the process tree, and retain bounded diagnostic evidence.
2. [x] Add output-budget regressions. Limit individual input lines and in-memory stdout; return bounded stderr tails with truncation metadata. Keep sanitized disk logs and do not parse truncated successful results. Command protocol capture remains independent of sanitized logs.
3. [x] Add redaction correctness and long-text regressions; replace quadratic scheme matching without exposing credentials.
4. [x] Batch verification startup initialization and pending discovery. Preserve inferred legacy status, retry deadlines, and existing verification evidence.
5. [x] Replace the fixed 50ms scheduler loop with wakeups and a bounded fallback; skip full-slot queue reads. Verify submit, completion, cancellation, retries and shutdown.
6. [x] Use one canonical workspace key for admission, quarantine and scheduling; reject case aliases consistently on Windows.
7. [x] Run full Python/frontend regression, build and inspect the wheel, and check the diff. Submit the implementation snapshot to Cursor in read_only mode; verify material feedback and report further opportunities separately from implemented changes. Cursor task 6214870c0ff84c5095a102db6cfb3bd7 completed; two P2 findings and the Muse full-log reread P3 were reproduced and fixed afterward. Final Python: 510 cases, 505 passed, 5 platform skips; frontend: 19 passed. Rebuilt wheel: 48 source/assets byte-identical. Cursor did not review the later correction bytes; those have local regression evidence.

**Implementation choices:** stdout collection budget 8 MiB, individual protocol line budget 4 MiB, stderr return tail 64 KiB. Oversized stdout/protocol records fail explicitly rather than silently parsing incomplete results. These are local implementation defaults, not provider limits.

**Validation:** Each fix starts with a failing regression. Use isolated fake executors and temporary data. Full suite uses fail-fast to surface diagnostics promptly. Cursor is a static reviewer; its review is not a replacement for measured tests.
