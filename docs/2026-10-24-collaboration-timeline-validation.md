# Collaboration timeline validation — 2026-10-24

## Scope

Unified chronological timeline of public collaboration messages and observed/reported file/command/test operations, with per-card and loaded-only bulk expand/collapse. Spec: `docs/superpowers/specs/2026-10-24-collaboration-timeline-design.md`. Plan tasks 1–5 implemented in the isolated collaboration-timeline worktree. No merge, push, daily Hub restart, global hooks, or private chat-library scraping.

## Developer note (Cursor)

Cursor CLI in this turn was restricted to Read/Write only (no Shell/MCP/WebFetch). Implementation and tests were authored; **commands below were not executed in the developer session**. Controller must run them and return failures for repair.

## Suggested verification commands

```sh
# Prefer short TEMP/TMP on Windows if NodeTransferTests fail on long worktree paths.
# Directed (after this repair; expect ~72+ including work_items / acceptance):
python -m unittest test_activity test_stream_events test_work_items test_dashboard test_dashboard_chains test_dashboard_timeline test_dashboard_http test_acceptance_dashboard -v
node --test tests/frontend/*.mjs
# Full suite (was ~440; must re-run after directed pass):
python -m unittest discover -s tests
git diff --check
python -m pip wheel . --no-deps
```

### Expected outcomes

- `test_activity`: normalize whitelist/byte budgets; concurrent identical `request_id` → one event; conflict on changed payload; terminal append leaves `state`/`updated`/verification unchanged; stale controller rejected; MCP tool count **20** including `create_work_item`, `link_task`, `reconcile_task`, `record_activity`.
- `test_stream_events`: Codex/Claude/Cursor synthetic shapes extract safe fields before `log_record` redaction; command v1 fake `hub_event`/stdout still ignored; mere `test` substring ≠ test evidence.
- `test_dashboard_timeline`: mixed chronology; call-id pairing within task only; cross-task IDs never merge; budgets include nested operation fields; hostile paths redacted.
- Frontend: expansion individual/bulk/future arrivals/chain isolation; app still safe-`textContent` and graph/details interactions.
- Wheel contains `dashboard_timeline.py` and `dashboard_static/timeline.mjs`.

## Synthetic browser preview (controller)

```sh
python scripts/synthetic_collaboration_timeline.py --data-dir %TEMP%\hub-synth-timeline
# point AGENT_HUB_DATA_DIR at that directory, start daemon, open dashboard
```

Fixture marker: `SYNTHETIC_COLLABORATION_TIMELINE_DEMO`. Expect two-model chain (codex ↔ grokbot review + rework) with read/command/test/file_change and hostile HTML text rendered as text. **This is synthetic acceptance, not live automatic tool capture.**

## Capture coverage (honest)

| Source | What is collected | What is not |
| --- | --- | --- |
| Codex/Claude/Cursor CLI | Structured tool/message fields mapped before log redaction | Unrecognized shapes → generic summary only; no full file bodies |
| Grok Bot | Public assistant `send-message` after task baseline, seq/revision dedup | `thought`, foreign roles/sessions, unpublished tool params |
| Muse | Owned visible completed display messages after baseline | Cross-session history, hidden/thought payloads; completion/human-wait rules unchanged |
| command v1 | Result protocol only | No stdout event parsing / fake hub_event |
| External `record_activity` | Authenticated controller-reported whitelist activities | Does not auto-hook desktop clients |

## Unverified in developer session

- Actual unittest / node / wheel exit codes and durations (Cursor developer session: no Shell; do not treat code presence as green)
- Directed suite above + full discover + frontend + browser after refresh/new arrivals
- Live Grok/Muse provider tool-parameter availability beyond public messages
- Production reconcile against real Grok history (synthetic page fixtures cover algorithm)

## Boundaries retained

No expansion of execution/cancel/controller/frontend write powers. Grok/Muse completion, cancel, ownership, human-wait unchanged. Path redaction kept; no private chat DB; no credentials/env dumps in timeline records.
