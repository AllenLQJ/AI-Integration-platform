#!/usr/bin/env python3
"""Build an isolated synthetic two-model collaboration timeline for dashboard preview.

Uses a temporary data directory only. Does not start adapters, contact networks,
or read private chat databases. Explicitly labeled SYNTHETIC — not live capture.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from agent_hub.activity import normalize_activity
from agent_hub.store import Store
from agent_hub.verification import Verification


SYNTHETIC_MARKER = 'SYNTHETIC_COLLABORATION_TIMELINE_DEMO'


def build(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / 'tasks.db')
    Verification(store, None)
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    (workspace / 'src').mkdir(exist_ok=True)
    (workspace / 'src' / 'app.py').write_text('print("hi")\n', encoding='utf-8')

    coder = store.create('synth-coder-1', dict(
        prompt=f'{SYNTHETIC_MARKER}\n请实现 app 问候语并跑测试。',
        agent='codex', mode='edit_worktree', cwd=str(workspace)))
    store.event(coder['id'], 'message', {
        'message': '先读取 src/app.py，再补充测试。', 'role': 'assistant',
        'provider_message_id': 'c-1', 'provider_revision': 1})
    store.event(coder['id'], 'activity', normalize_activity({
        'kind': 'file_read', 'operation_id': 'read-1', 'status': 'started', 'phase': 'started',
        'actor': 'codex', 'operation': {'tool_name': 'Read', 'path': str(workspace / 'src' / 'app.py')},
    }, str(workspace), force_provenance='executor_observed'))
    store.event(coder['id'], 'activity', normalize_activity({
        'kind': 'file_read', 'operation_id': 'read-1', 'status': 'completed', 'phase': 'completed',
        'actor': 'codex', 'operation': {'tool_name': 'Read', 'path': str(workspace / 'src' / 'app.py'),
                                        'read_range': {'start': 1, 'end': 20}},
    }, str(workspace), force_provenance='executor_observed'))
    store.event(coder['id'], 'activity', normalize_activity({
        'kind': 'command', 'operation_id': 'cmd-1', 'status': 'completed',
        'actor': 'codex',
        'operation': {
            'tool_name': 'Shell',
            'command_display': 'pytest -q',
            'exit_code': 0,
            'stdout_excerpt': '1 passed',
            'test_summary': 'passed=1 failed=0',
        },
    }, str(workspace), force_provenance='executor_observed'))
    # Hostile text must render as textContent safely in the browser.
    store.event(coder['id'], 'message', {
        'message': '<img src=x onerror=alert(1)> password=not-a-real-secret',
        'role': 'assistant', 'provider_message_id': 'c-2', 'provider_revision': 1})
    store.finish(coder['id'], 'succeeded', {'answer': f'实现完成。\n{SYNTHETIC_MARKER}'})

    review = store.create('synth-review-1', dict(
        prompt=f'{SYNTHETIC_MARKER}\n请审查实现并指出返修点。',
        agent='grokbot', mode='delegate', cwd=str(workspace),
        review_of={'task_id': coder['id']}))
    store.event(review['id'], 'message', {
        'message': '测试通过，但问候文案需要更清晰。', 'role': 'assistant',
        'provider_message_id': 'g-1', 'provider_revision': 1})
    store.event(review['id'], 'activity', normalize_activity({
        'kind': 'message', 'actor': 'controller', 'title': '派发审查',
        'text': '请重点检查文案与测试覆盖。',
    }, str(workspace), force_provenance='controller_reported'))
    store.finish(review['id'], 'succeeded', {'answer': f'建议返修文案。\n{SYNTHETIC_MARKER}'})

    rework = store.create('synth-rework-1', dict(
        prompt=f'{SYNTHETIC_MARKER}\n按审查意见返修文案。',
        agent='codex', mode='edit_worktree', cwd=str(workspace),
        parent_id=coder['id'], resume_session='synth-session'))
    store.event(rework['id'], 'activity', normalize_activity({
        'kind': 'file_change', 'operation_id': 'chg-1', 'status': 'completed',
        'operation': {'path': 'src/app.py', 'change_summary': '更新问候文案', 'tool_name': 'Edit'},
    }, str(workspace), force_provenance='executor_observed'))
    store.event(rework['id'], 'activity', normalize_activity({
        'kind': 'test', 'operation_id': 'test-1', 'status': 'completed',
        'operation': {'command_display': 'pytest -q', 'exit_code': 0, 'test_summary': 'passed=2 failed=0'},
    }, str(workspace), force_provenance='executor_observed'))
    store.finish(rework['id'], 'succeeded', {'answer': f'返修完成。\n{SYNTHETIC_MARKER}'})

    meta = {
        'marker': SYNTHETIC_MARKER,
        'data_dir': str(root),
        'chain_member_id': review['id'],
        'tasks': {'coder': coder['id'], 'review': review['id'], 'rework': rework['id']},
        'note': 'Synthetic fixture only. Does not prove live provider tool capture.',
    }
    (root / 'synthetic-timeline.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', help='optional persistent temp-like directory; default uses mkdtemp')
    args = parser.parse_args()
    if args.data_dir:
        root = Path(args.data_dir)
        meta = build(root)
    else:
        tmp = tempfile.mkdtemp(prefix='hub-synth-timeline-')
        meta = build(Path(tmp))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print('\nPreview (isolated daemon required; controller runs this):')
    print(f"  set AGENT_HUB_DATA_DIR={meta['data_dir']}")
    print('  python -m agent_hub daemon')
    print('  python -m agent_hub dashboard')


if __name__ == '__main__':
    main()
