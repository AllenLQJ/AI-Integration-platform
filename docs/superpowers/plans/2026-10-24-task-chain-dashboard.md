# Task Chain Dashboard Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Present related executions as one task chain with linked graph and conversation.
**Architecture:** Add a read-only chain projection and additive HTTP endpoints; keep frontend graph rendering separate from API and state. Reuse scoped authentication, redaction and existing execution detail/events.
**Tech Stack:** Python stdlib/SQLite, native JavaScript modules, CSS/SVG; no new dependencies.
**Spec:** docs/superpowers/specs/2026-10-24-task-chain-dashboard.md

## Global Constraints
- Portable code, no personal paths or secrets in distributable sources.
- Read-only projection, no new Service or adapter/model calls against live data.
- Preserve existing API and authentication behavior.

## Review Focus
- Branches, reviews-of-reviews, cycles and missing ancestors terminate deterministically.
- Filtering/pagination cannot split chains or duplicate them.
- Status distinguishes execution success, acceptance and in-progress reviews.
- Poll races and newly added nodes must not reset viewport or leave stale detail.
- Long chains/messages and mobile layouts remain bounded and disclose omissions.

## Task 1: Backend chain projection
Files: new src/agent_hub/dashboard_chains.py, tests/test_dashboard_chains.py; modify dashboard_http.py, tests/test_dashboard_http.py.
Interfaces: DashboardChains(dashboard).chains(limit=30,offset=0,q='',state='',agent='') and .detail(member_id), per spec DTO.
- [x] Add and run failing tests for grouping, filters, cycles/dangling, ordering/status, redaction, caps and read-only behavior.
- [x] Implement projection and authenticated additive routes.
- [x] Run chain/dashboard/HTTP tests and record results.

## Task 2: Graph and conversation frontend
Files: dashboard_static/app.mjs, api.mjs, state.mjs, index.html, style.css; new graph.mjs; tests/frontend/*.test.mjs.
Interfaces: chain API per spec; graph renderer owns viewport and selection, invokes callback(task_id); existing task detail/events serve selected node.
- [x] Add and run failing tests for API shape, graph layout (branches/cycles), linked selection and stale requests.
- [x] Implement chain list, graph controls, source-labeled conversation and selected-node details/events.
- [x] Register graph.mjs in static whitelist; run frontend tests.

## Task 3: Integration and review
Files: docs/dashboard.md, docs/validation/2026-10-24-task-chain-validation.md.
- [x] Run full Python and frontend suites; inspect diff with independent reviewer.
- [x] Start isolated fixture preview, verify desktop/mobile graph, node/conversation linkage and polling in real browser. No model calls.
- [x] Fix confirmed findings, re-run affected tests, document limits and evidence.
- [x] Commit feature branch and deliver preview; retain worktree for review, no automatic merge/push.

## Task 4 (appended): work_item_id + reconcile
Files: work_items.py, service/mcp/client, dashboard_chains, grokbot.reconcile, gateway result_fields, tests/test_work_items.py, docs.
- [x] Implement create/link/submit-require/inherit, GET-only reconcile, uncertain UI/gateway_state, docs.
- [ ] Controller runs full test suite + browser; Grok re-review.

