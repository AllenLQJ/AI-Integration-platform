# Task-chain dashboard validation — 2026-10-24

## Scope

User-approved task-chain list, explicit execution graph, unified chronological conversation and selected-node details. Branch based on main 5c01229. No changes to executor protocols, scheduling or live task data. No manual grouping by inferred intent.

## Automated checks

- Baseline:26 dashboard Python tests and8 frontend tests passed. Sandboxed Windows temporary-directory permissions required running tests outside the restricted environment; an initial loopback connection abort did not recur on baseline rerun.
- New dashboard subset:37 tests passed,0 skipped,27.153seconds. Includes grouping, reviews/continuations, filter pagination, cycles/malformed/missing relationships, message provenance/redaction and caps, authenticated HTTP routing and new static resource.
- Frontend:11 tests passed,0 skipped. Actual app module checks new-member discovery with no selected-node events, request races, exact historical relation navigation, refreshed selection, preserved message/event DOM, graph position/focus, zoom/reset/collapse and safe text rendering.
- Full Python first run:405 run,396 passed,4 errors,5 platform skips. All errors were NodeTransferTests under the long worktree temp path. Shortening only TEMP/TMP made all18 NodeTransferTests pass. No product-source workaround was added.
- Full Python repeat:405 run,400 passed,5 platform skips,226.995seconds; exit0.
- Offline local wheel build succeeded. dashboard_chains.py and all6 static assets (including graph.mjs) are packaged; packaged dashboard sources match working files byte for byte.

## Independent review

One P2 was found: navigating an older related execution outside the200-node graph window selected the latest execution instead. Regression reproduced failure before the fix. Explicit desired-node navigation and selected-member polling now preserve the target through refresh and capped responses. Reviewer confirmed the fix,11 frontend tests, and an additional in-memory stale-response reproduction passed. No remaining review findings.

## Browser checks

Isolated fixture preview used actual HTTP/authentication/projection/frontend with synthetic records only. It instantiated no Service scheduler, invoked no adapters or models and did not read or mutate live Hub data.

- Seven related executions form one list item; one unrelated task remains separate.
- Graph contains7 nodes and6 correctly typed edges after an added fixture continuation.
- Unified conversation contains14 messages with task and executor provenance.
- Clicking review node selects its right detail and positions its conversation; events tab shows only that execution.
- Added seventh execution without refreshing the page: graph and conversation updated while selected review node remained unchanged.
- Zoom, reset, collapse and detail visibility controls work.
- 390 CSS-pixel viewport: document width390 (no page overflow), task drawer opens/closes, collapse enables conversation reading. Browser emulation restored afterwards.
- Browser warning/error log empty at inspection.

Screenshots are retained as local review artifacts, not embedded in distributable source. Preview records are explicitly labeled demonstrations; they do not establish live model quality or live acceptance.

## Boundaries

Graph200nodes, conversation500messages/1000000textcharacters; existing individual-message caps remain. Warnings disclose caps and invalid relationships. Component construction scans relation metadata per request; no relation index/cache yet. Unlinked historical tasks remain independent. Feature worktree retained for review; no main merge, remote push, live daemon replacement or real execution occurred.

