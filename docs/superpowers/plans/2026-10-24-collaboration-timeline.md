# Collaboration timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task. User-selected execution: Cursor CLI develops; controller runs commands/tests; Grok Bot independently reviews the final diff. Steps use checkbox syntax.

**Goal:** Show model conversations and observed/reported file, command and test operations in one chronological, individually and collectively collapsible timeline.
**Architecture:** A bounded, sanitized activity contract feeds append-only Hub events and an additive dashboard projection. Existing task ownership/lifecycle/verification stays authoritative. An authenticated record_activity writer supports external controllers; native ES modules render stable cards.
**Tech Stack:** Python >=3.10, SQLite, unittest, native ES modules, node:test; no required new dependency.
**Spec:** docs/superpowers/specs/2026-10-24-collaboration-timeline-design.md (approved by user's instruction to hand development to Cursor and review to Grok Bot).

## Global Constraints

- Preserve existing three-column dashboard, graph, readonly browser session and existing APIs.
- Keep explicit parent_id/review_of links; never infer identity, operation pairing or task relationships from prose.
- Preserve CLI/command-v1, completion marker, remote ownership, cancellation, uncertainty and human-wait contracts.
- No thought/private history capture, credentials, full file contents, account changes or global hooks.
- Provenance executor_observed/controller_reported/legacy_summary must remain visible; unsupported capture is explicitly unavailable.
- 200 nodes, 500 loaded timeline records and 1,000,000 total display characters, including operation fields.
- record_activity <=65,536 UTF-8 JSON bytes; output excerpts <=16,384 UTF-8 bytes combined. Sanitize before excerpt truncation; strict scalar bounds.
- Runtime records and private machine paths stay outside tracked sources/distributions. Use synthetic fixtures.
- No merge, push, daily Hub restart or global configuration change. Cursor cannot execute commands; controller performs validation.

## Review Focus

1. Concurrent retries/changed payloads/expired controller epochs: one atomic event receipt or explicit error (Task 1).
2. Missing call IDs, amended accumulated messages, foreign sessions and hidden thought entries: no guessed pairing/duplicate/leak (Task 2).
3. Many large operations and adverse device timestamps: stable bounded projection including nested fields (Task 3).
4. Refresh, started-to-completed replacement, new arrivals and return to a chain: preserve individual/bulk expansion choices (Task 4).
5. Readonly session and hostile text/paths/credentials: no writes, execution, unsafe rendering or relaxed redaction (Tasks 1, 3, 4).

### Task 1: Activity contract and authenticated writer

**Files:** create src/agent_hub/activity.py and tests/test_activity.py; modify store.py, service.py, mcp_server.py, __main__.py, relevant MCP/HTTP tests and docs.
**Interfaces:** normalize_activity(activity: dict, workspace: str = '') -> dict validates and sanitizes whitelisted fields; Store.record_activity(request_id, task_id, event_id, activity) -> dict atomically appends kind=activity plus independent receipt; Service.call('record_activity', args) checks strict arguments/controller under existing task lock. MCP signature record_activity(request_id: str, task_id: str, event_id: str, activity: dict, controller_id: str | None = None, controller_epoch: int | None = None) -> dict. CLI exposes existing rpc record_activity JSON; no new execution endpoint.
- [ ] Add tests: allowed kinds/fields/types, bytes not codepoints, empty/oversized/unknown inputs rejected; missing task rejected.
- [ ] Add tests: concurrent identical requests append once; changed content or event identity conflicts; invalid retries cannot evade lease check; terminal append does not change state, updated or verification.
- [ ] Controller runs python -m unittest discover -s tests -p test_activity.py -v before/after implementation and shares failures with Cursor.
- [ ] Implement safe relative-path display, bounded scalar/output policy, transactional receipt table and existing management-auth/lease integration. Force external provenance; never accept caller-selected trusted provenance.
- [ ] Add MCP/CLI registration and capability/tool-count changes, readonly-cookie rejection test. Stage only intended files; controller commits after checks.

### Task 2: Observed executor events and public progress

**Files:** modify processes.py, adapters/grokbot.py, adapters/muse.py only where task ownership is established; extend tests/test_processes.py, tests/test_cursor_grokbot.py and applicable Muse regressions.
**Interfaces:** stream_events(data) keeps existing message/tool/session events compatible and enriches safe fields or emits normalized activity. Use real operation IDs and explicit provider message revisions. Extraction precedes current log_record field removal; raw logs remain restricted. No arbitrary stdout parsing.
- [ ] Add real-shape synthetic Codex/Claude/Cursor samples for file-read metadata, command exits/output, explicit test summaries, file changes, Unicode, missing IDs/types and unknown records.
- [ ] Add tests for safe extraction before logging redaction; command v1 fake hub_event/stdout remains ignored. Never treat a mere test substring as actual test evidence.
- [ ] Add Grok owned-visible-send-message baseline/revision dedup tests; hidden thought/foreign session ignored; old final-marker/cancel/idle/uncertainty regressions retained.
- [ ] Add Muse owned-message regression cases without weakening its existing completion or human-wait semantics. Mark unsupported capture accurately rather than fabricate.
- [ ] Controller runs affected unittest modules before/after implementation; Cursor implements fixes using the test evidence.

### Task 3: Bounded timeline projection and compatibility

**Files:** create src/agent_hub/dashboard_timeline.py, tests/test_dashboard_timeline.py; modify dashboard.py, dashboard_chains.py and related dashboard tests.
**Interfaces:** task_timeline(task, messages, events) -> dict with timeline and capture_coverage; bounded event retrieval from Store; chain detail adds timeline/capture_coverage while retaining messages/nodes/edges/warnings. Each entry has stable id/task_id/kind/timestamp/source/completeness/provenance and optional operation/actor/recipient/status/phase/source_event_ids/truncated_fields.
- [ ] Add tests for one mixed chronological chain, updates paired only on actual call IDs, missing IDs remain distinct, cross-task IDs never merge, provider revision dedup and stable receipt-seq ordering.
- [ ] Add tests for legacy-only data, no operations recorded vs capture unavailable, final-answer dedup, explicit relationships only and unchanged old API keys.
- [ ] Add budget tests with >500 records, >200 nodes, >1,000,000 nested display chars and adversarial fields; bounded queries and explicit truncation.
- [ ] Implement normalized safe projection with workspace-relative file metadata; preserve global dashboard _text redaction. Include entire display payload in shared budgets.
- [ ] Controller runs dashboard unittest modules before/after implementation and records unresolved capture limits.

### Task 4: Timeline cards and expansion state

**Files:** create dashboard_static/timeline.mjs and tests/frontend/timeline.test.mjs; modify app.mjs, state.mjs, index.html, style.css, api.mjs and existing frontend tests as needed.
**Interfaces:** export TimelineExpansion with isOpen(chainId, record), setOpen(chainId,id,open), setAll(chainId,open); state separated from DOM reuse and keyed by stable chain/entry IDs. Export pure projection/helper functions where useful for meaningful node tests. Frontend chooses chain.timeline if present, otherwise legacy messages.
- [ ] Add tests: individual and all-loaded toggles; bulk policy applies to future arrivals; later individual overrides survive updates; chain isolation/return restoration; graph target opens only target.
- [ ] Add compatibility tests: old service messages, async response races, empty/malformed optional fields and polling updates. Keep existing graph/details interactions.
- [ ] Controller runs node --test tests/frontend/*.mjs before/after implementation.
- [ ] Implement accessible full-card details/summary or equivalent with keyboard and aria-expanded, separate details button, actor/time/type/state/preview header and bounded safe output body.
- [ ] Add top controls 全部展开 / 全部折叠 labelled 已载入; show coverage/truncation. Short messages default expanded, long messages/operations collapsed; document chosen threshold.
- [ ] Verify textContent rendering, focus retention, narrow-screen wrapping/scroll, reduced motion and refresh preservation.

### Task 5: Portable integration docs and synthetic acceptance

**Files:** update docs/dashboard.md, README.md/README.zh-CN.md if present, tool-contract docs/count tests; create a portable synthetic timeline demonstration fixture/script and a validation note.
**Interfaces:** record_activity calls through existing Client; explicit synthetic marker; no network/model/private runtime dependencies; isolated temporary data root only.
- [ ] Document supported source coverage, external writer usage/auth/leases/idempotence, relative paths/limits, MCP refresh, loaded-only bulk controls and historical limitations.
- [ ] Supply a runnable two-model synthetic read/command/test/review/rework chain with operations at starts/completions and hostile text fixture.
- [ ] Controller launches isolated preview, verifies desktop/narrow screen and individual/bulk folding after refresh and new arrivals; collect evidence outside source.
- [ ] Controller runs full Python unittest suite, frontend node tests, git diff --check and wheel/static asset checks. Record passed/skipped/unverified separately.

### Task 6: Cursor delivery, Grok review and repair

**Files:** only in-scope code/tests/docs; no live config, task databases or private evidence.
- [ ] Cursor reports changed files, choices, coverage gaps and tests it could not execute; controller records actual command output and commits in isolated worktree.
- [ ] Submit Grok Bot a Hub-tracked review pointing to exact local worktree, base/head and spec/plan; it reads local files via its existing Windows connection, with no source dump.
- [ ] Grok reviews real diff and tests for priority defects, contract/security regressions and user requirements; source locations, trigger, impact and validation required.
- [ ] Send material confirmed findings to Cursor; controller re-tests, Grok re-reviews changed scope. Do not relabel synthetic data as automatic live-provider capture.
- [ ] Present final diff, developer/reviewer task IDs, validation results and remaining limits to user. Retain branches/worktrees.

## Self-review

### 后续集成检查

- [x] 补稳定 work_item_id、显式审核/返修关系、历史关联及并发幂等测试。
- [x] 补 GET-only Grok 原任务恢复、崩溃落盘顺序、进度持久化和条件状态文案。
- [x] 补友好身份标题、普通对话策略、测试/页面/独立审核验收证据展示。
- [x] 总控复现并修复静态模块白名单与测试夹具；新增本地依赖真实 HTTP 检查及网关回归。
- [ ] 最终完整 Python、前端、浏览器、wheel 验证及 Grok 复审；通过后填写真实计数，保留平台跳过与未接入来源限制。

Tasks 1-3 cover capture/auth/projection and budgets; Task 4 covers all fold and compatibility requirements; Task 5 covers portable docs, demo and full validation; Task 6 preserves requested independent reviewer. All five review focus classes map to explicit tests. Concrete internal names above are proposed interfaces; Cursor may make small coherent adjustments when source evidence requires, documenting them without changing the approved behavior.
