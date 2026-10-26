# Reliability and dashboard performance

Implement the five recommendations approved in the chat on 2026-10-25.

1. Add regression tests for result write/replace failure and slot release. Make result publication atomic; always release execution slots, and persist a bounded failure inline when result publication fails. Preserve cancellation and remote uncertainty evidence.
2. Group work-item members with a representative instead of building a clique. Test independent groups, existing explicit edges and large synthetic histories.
3. Batch the bounded selected-node projection (tasks, verification summaries and timeline/message events), skip unused per-node relations, add relation indexes. Test output equivalence, event caps and a query budget. Reduce unchanged polling with a database revision that covers verification-only writes, while preserving authenticated read-only behavior.
4. Apply the existing title redaction and search work-item titles with literal LIKE escaping and same-member state/agent filters.
5. Diagnose and stabilize the reconcile test fixture without weakening the GET-only assertion. Add full Windows Python regression, Node frontend tests and wheel resource checks to CI while retaining the portable plugin matrix.

Validation: failing targeted tests before each implementation; directed suites; full Python discovery; Node tests; wheel resource inspection; git diff --check; final independent code review. Baseline: 468 Python cases, one intermittent reconcile failure, five platform skips; 15 frontend cases passed.

Implementation decision: execute directly in this workspace on codex/reliability-dashboard-performance; preserve unrelated untracked files. Use per-request batches rather than global mutable projection objects. No new dependencies or provider requests are required.
