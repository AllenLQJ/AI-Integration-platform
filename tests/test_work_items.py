import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

from agent_hub.models import HubError
from agent_hub.service import Service
from agent_hub.work_items import conversation_acceptance, software_dashboard_acceptance
from hub_fixtures import ensure_work_item, with_work_item
from test_service import Fake


class WorkItemProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root / 'data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def test_submit_requires_work_item_and_never_silent_root(self):
        with self.assertRaises(HubError) as err:
            self.service.call('submit_task', dict(
                request_id='no-wi', agent='fake', prompt='x', cwd=str(self.root), mode='read_only'))
        self.assertEqual(err.exception.code, 'WORK_ITEM_REQUIRED')
        self.assertIsNone(self.service.store.by_request('no-wi'))

    def test_create_dispatch_review_rework_same_work_item(self):
        wid = ensure_work_item(self.service, 'wi-chain')
        dev = self.service.call('submit_task', dict(
            request_id='dev', agent='fake', prompt='implement', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='development'))
        review = self.service.call('submit_task', dict(
            request_id='rev', agent='fake', prompt='review please', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='review', related_task_id=dev['id']))
        rework = self.service.call('submit_task', dict(
            request_id='rw', agent='fake', prompt='fix', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='rework', related_task_id=dev['id']))
        members = {m['task_id']: m for m in self.service.work_items.members(wid)}
        self.assertEqual(members[dev['id']]['relation'], 'development')
        self.assertEqual(members[review['id']]['relation'], 'review')
        self.assertEqual(members[review['id']]['related_task_id'], dev['id'])
        self.assertEqual(members[rework['id']]['relation'], 'rework')
        # Independent feature with same prompt title must not auto-merge.
        other = ensure_work_item(self.service, 'wi-other', title='协作与操作统一时间线')
        self.assertNotEqual(wid, other)
        lone = self.service.call('submit_task', dict(
            request_id='lone', agent='fake', prompt='implement', cwd=str(self.root),
            mode='read_only', work_item_id=other))
        self.assertEqual(self.service.work_items.membership(lone['id'])['work_item_id'], other)

    def test_review_without_related_rejected(self):
        wid = ensure_work_item(self.service, 'wi-bad-rev')
        with self.assertRaises(HubError) as err:
            self.service.call('submit_task', dict(
                request_id='bad-rev', agent='fake', prompt='r', cwd=str(self.root),
                mode='read_only', work_item_id=wid, relation='review'))
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')

    def test_link_task_audit_idempotent_lease_and_cross_item_reject(self):
        wid = ensure_work_item(self.service, 'wi-link')
        a = self.service.call('submit_task', with_work_item(self.service, dict(
            request_id='link-a', agent='fake', prompt='a', cwd=str(self.root), mode='read_only'),
            request_prefix='wi-a'))
        # Create orphan historical task via store (pre-protocol).
        orphan = self.service.store.create('orphan', dict(
            agent='fake', prompt='old', cwd=str(self.root), mode='read_only'))
        lease = self.service.call('claim_controller', dict(
            request_id='claim-link', task_id=orphan['id'], controller_id='ctrl'))
        with self.assertRaises(HubError) as err:
            self.service.call('link_task', dict(
                request_id='link-1', work_item_id=wid, task_id=orphan['id'], relation='development',
                controller_id='wrong', controller_epoch=lease['controller_epoch']))
        self.assertEqual(err.exception.code, 'CONTROLLER_STALE')
        first = self.service.call('link_task', dict(
            request_id='link-1', work_item_id=wid, task_id=orphan['id'], relation='development',
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        second = self.service.call('link_task', dict(
            request_id='link-1', work_item_id=wid, task_id=orphan['id'], relation='development',
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertEqual(first, second)
        events = [e for e in self.service.store.events(orphan['id'])['events'] if e['kind'] == 'work_item_link']
        self.assertEqual(len(events), 1)
        other = ensure_work_item(self.service, 'wi-link-2')
        lease_a = self.service.call('claim_controller', dict(
            request_id='claim-a', task_id=a['id'], controller_id='ctrl-a'))
        with self.assertRaises(HubError) as err:
            self.service.call('link_task', dict(
                request_id='link-cross', work_item_id=other, task_id=a['id'], relation='linked',
                controller_id=lease_a['controller_id'], controller_epoch=lease_a['controller_epoch']))
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')
        # A NEW, unattached task must still reject a self-edge as INVALID_ARGUMENT.
        # `orphan` above is already linked; its changed relation is covered by
        # the separate existing-member REQUEST_CONFLICT regression below.
        self_candidate = self.service.store.create('fresh-self', dict(
            agent='fake', prompt='new self-edge candidate', cwd=str(self.root), mode='read_only'))
        self_lease = self.service.call('claim_controller', dict(
            request_id='claim-fresh-self', task_id=self_candidate['id'], controller_id='self-ctrl'))
        with self.assertRaises(HubError) as err:
            self.service.call('link_task', dict(
                request_id='link-self', work_item_id=wid, task_id=self_candidate['id'],
                relation='linked', related_task_id=self_candidate['id'],
                controller_id=self_lease['controller_id'], controller_epoch=self_lease['controller_epoch']))
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')
        self.assertIsNone(self.service.work_items.membership(self_candidate['id']))

    def test_declared_independent_reviewer_projection(self):
        # Historical fixtures: full fields + disabled fake adapters; freeze to terminal
        # so the scheduler never starts them (no production executor softening).
        class Frozen:
            capabilities = ['edit_worktree', 'delegate', 'read_only']
            remote_cancel = False

            def diagnose(self):
                return {'available': False, 'capabilities': self.capabilities}

            def run(self, task, cancel, event):
                raise AssertionError('frozen historical fixture must not execute')

        self.service.adapters['cursor'] = Frozen()
        self.service.adapters['grokbot'] = Frozen()
        self.service.per_agent_concurrency['cursor'] = 0
        self.service.per_agent_concurrency['grokbot'] = 0

        def freeze(request_id, spec):
            task = self.service.store.create(request_id, dict(timeout_seconds=1800, **spec))
            self.service.store.finish(task['id'], 'succeeded', {'answer': 'historical fixture'})
            return self.service.store.get(task['id'])

        developer = freeze('dev-hist', dict(
            agent='cursor', prompt='feature timeline', cwd=str(self.root), mode='edit_worktree'))
        reviewer = freeze('rev-hist', dict(
            agent='grokbot', prompt='review feature', cwd=str(self.root), mode='delegate',
            context={'payload': {'role': 'independent_reviewer',
                                 'developer_task_id': developer['id']}}))
        # Same feature name alone must not merge.
        stranger = freeze('stranger', dict(
            agent='cursor', prompt='feature timeline', cwd=str(self.root), mode='edit_worktree'))
        self.assertEqual(developer['state'], 'succeeded')
        self.assertEqual(reviewer['state'], 'succeeded')
        projected = self.service.work_items.project_declared_review_pair(
            'proj-1', developer['id'], reviewer['id'])
        self.assertEqual(len(projected['members']), 2)
        self.assertIsNone(self.service.work_items.membership(stranger['id']))
        self.assertEqual(developer['state'], self.service.store.get(developer['id'])['state'])
        self.assertEqual(reviewer['request_id'], 'rev-hist')
        self.assertEqual(self.service.store.get(developer['id'])['state'], 'succeeded')
        self.assertEqual(self.service.store.get(reviewer['id'])['state'], 'succeeded')

    def test_dashboard_aggregates_work_item_not_review_state(self):
        from agent_hub.dashboard import Dashboard
        from agent_hub.dashboard_chains import DashboardChains
        wid = ensure_work_item(self.service, 'wi-dash', title='协作与操作统一时间线')
        dev = self.service.call('submit_task', dict(
            request_id='dash-dev', agent='fake', prompt='ship it', cwd=str(self.root),
            mode='read_only', work_item_id=wid))
        self.service.store.finish(dev['id'], 'succeeded', {'answer': 'done'})
        rev = self.service.call('submit_task', dict(
            request_id='dash-rev', agent='fake', prompt='review', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='review', related_task_id=dev['id']))
        self.service.store.finish(rev['id'], 'failed', {'error': 'reviewer failed', 'error_code': 'X'})
        chains = DashboardChains(Dashboard(self.service)).chains()
        item = next(c for c in chains['items'] if c.get('work_item_id') == wid)
        self.assertEqual(item['title'], '协作与操作统一时间线')
        self.assertEqual(item['latest_task_id'], dev['id'])
        self.assertEqual(item['state'], 'succeeded')
        detail = DashboardChains(Dashboard(self.service)).detail(wid)
        kinds = {e['kind'] for e in detail['edges']}
        self.assertIn('review', kinds)

    def test_client_work_item_helper_carries_id(self):
        from agent_hub.client import WorkItemClient
        wid = ensure_work_item(self.service, 'wi-client')
        helper = WorkItemClient(self.service, wid)

        class Proxy:
            def call(self, tool, arguments=None):
                return self_outer.service.call(tool, arguments)

        self_outer = self
        helper.client = Proxy()
        task = helper.dispatch('cli-dev', 'fake', 'p', str(self.root), 'read_only')
        self.assertEqual(self.service.work_items.membership(task['id'])['work_item_id'], wid)

    def test_continue_inherits_work_item(self):
        wid = ensure_work_item(self.service, 'wi-cont')
        parent = self.service.call('submit_task', dict(
            request_id='cont-p', agent='fake', prompt='hello', cwd=str(self.root),
            mode='read_only', work_item_id=wid))
        self.service.store.update(parent['id'], state='succeeded', session_id='native-123',
                                  workspace=str(self.root))
        # continue_task is controller-gated when a lease exists; none claimed here.
        child = self.service.call('continue_task', dict(
            request_id='cont-c', task_id=parent['id'], prompt='next'))
        member = self.service.work_items.membership(child['id'])
        self.assertEqual(member['work_item_id'], wid)
        self.assertEqual(member['relation'], 'continuation')

    def test_link_task_concurrent_conflict_has_no_side_effects(self):
        """Same request_id, different task content: loser must roll back — no dual members/audit."""
        wid = ensure_work_item(self.service, 'wi-conc', title='A')
        tasks = []
        for name in ('orphan-conc-a', 'orphan-conc-b'):
            task = self.service.store.create(name, dict(
                agent='fake', prompt=name, cwd=str(self.root), mode='read_only',
                timeout_seconds=1800))
            self.service.store.finish(task['id'], 'succeeded', {'answer': 'done'})
            tasks.append(self.service.store.get(task['id']))
        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        def worker(index, task_id):
            try:
                barrier.wait(2)
                results[index] = self.service.work_items.link_task(
                    'link-race-shared', wid, task_id, relation='linked')
            except HubError as err:
                errors[index] = err

        threads = [
            threading.Thread(target=worker, args=(0, tasks[0]['id'])),
            threading.Thread(target=worker, args=(1, tasks[1]['id'])),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        outcomes = []
        for i in range(2):
            if results[i] is not None:
                outcomes.append('ok')
            elif errors[i] is not None:
                outcomes.append(errors[i].code)
            else:
                self.fail('worker %s produced neither result nor error' % i)
        self.assertEqual(sorted(outcomes), ['REQUEST_CONFLICT', 'ok'])
        members = self.service.work_items.members(wid)
        self.assertEqual(len(members), 1)
        linked_id = members[0]['task_id']
        other_id = tasks[0]['id'] if linked_id == tasks[1]['id'] else tasks[1]['id']
        self.assertIsNone(self.service.work_items.membership(other_id))
        events = [e for e in self.service.store.events(linked_id)['events'] if e['kind'] == 'work_item_link']
        self.assertEqual(len(events), 1)
        other_events = [e for e in self.service.store.events(other_id)['events'] if e['kind'] == 'work_item_link']
        self.assertEqual(len(other_events), 0)

    def test_submit_relation_change_conflicts_and_cycle_rejected(self):
        wid = ensure_work_item(self.service, 'wi-rel')
        a = self.service.call('submit_task', dict(
            request_id='rel-a', agent='fake', prompt='a', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='development'))
        b = self.service.call('submit_task', dict(
            request_id='rel-b', agent='fake', prompt='b', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='development'))
        with self.assertRaises(HubError) as err:
            self.service.call('submit_task', dict(
                request_id='rel-a', agent='fake', prompt='a', cwd=str(self.root),
                mode='read_only', work_item_id=wid, relation='review', related_task_id=b['id']))
        self.assertEqual(err.exception.code, 'REQUEST_CONFLICT')
        self.assertEqual(self.service.work_items.membership(a['id'])['relation'], 'development')

        def hist(rid):
            task = self.service.store.create(rid, dict(
                agent='fake', prompt=rid, cwd=str(self.root), mode='read_only',
                timeout_seconds=1800))
            self.service.store.finish(task['id'], 'succeeded', {'answer': 'x'})
            return self.service.store.get(task['id'])

        x = hist('cyc-x')
        y = hist('cyc-y')
        z = hist('cyc-z')
        lease_x = self.service.call('claim_controller', dict(
            request_id='cyc-claim-x', task_id=x['id'], controller_id='opsx'))
        self.service.call('link_task', dict(
            request_id='link-x', work_item_id=wid, task_id=x['id'], relation='linked',
            controller_id=lease_x['controller_id'], controller_epoch=lease_x['controller_epoch']))
        lease_y = self.service.call('claim_controller', dict(
            request_id='cyc-claim-y', task_id=y['id'], controller_id='opsy'))
        self.service.call('link_task', dict(
            request_id='link-yx', work_item_id=wid, task_id=y['id'],
            relation='linked', related_task_id=x['id'],
            controller_id=lease_y['controller_id'], controller_epoch=lease_y['controller_epoch']))
        # Seed an existing a↔b loop in membership edges; new attach into that loop must fail.
        with self.service.store.connection(write=True) as db:
            db.execute('UPDATE work_item_members SET related_task_id=? WHERE task_id=?',
                       (y['id'], x['id']))
        lease_z = self.service.call('claim_controller', dict(
            request_id='cyc-claim-z', task_id=z['id'], controller_id='opsz'))
        with self.assertRaises(HubError) as err:
            self.service.call('link_task', dict(
                request_id='link-z-cycle', work_item_id=wid, task_id=z['id'],
                relation='linked', related_task_id=x['id'],
                controller_id=lease_z['controller_id'], controller_epoch=lease_z['controller_epoch']))
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')
        self.assertIsNone(self.service.work_items.membership(z['id']))
        # Historical ring member: same-content link is idempotent (must not raise cycle).
        before_events = len([e for e in self.service.store.events(x['id'])['events']
                             if e['kind'] == 'work_item_link'])
        same = self.service.call('link_task', dict(
            request_id='link-x-same', work_item_id=wid, task_id=x['id'],
            relation='linked', related_task_id=y['id'],
            controller_id=lease_x['controller_id'], controller_epoch=lease_x['controller_epoch']))
        self.assertTrue(same.get('already_linked'))
        self.assertEqual(self.service.work_items.membership(x['id'])['relation'], 'linked')
        self.assertEqual(self.service.work_items.membership(x['id'])['related_task_id'], y['id'])
        after_events = len([e for e in self.service.store.events(x['id'])['events']
                            if e['kind'] == 'work_item_link'])
        self.assertEqual(after_events, before_events)
        # Existing member: different relation must conflict without rewriting.
        with self.assertRaises(HubError) as err:
            self.service.call('link_task', dict(
                request_id='link-x-diff', work_item_id=wid, task_id=x['id'],
                relation='rework', related_task_id=y['id'],
                controller_id=lease_x['controller_id'], controller_epoch=lease_x['controller_epoch']))
        self.assertEqual(err.exception.code, 'REQUEST_CONFLICT')
        self.assertEqual(self.service.work_items.membership(x['id'])['relation'], 'linked')
        self.assertEqual(self.service.work_items.membership(x['id'])['related_task_id'], y['id'])

    def test_acceptance_templates_inherit_and_labels(self):
        from agent_hub.dashboard import Dashboard
        soft = software_dashboard_acceptance()
        wid = ensure_work_item(self.service, 'wi-acc', title='看板开发', acceptance=soft)
        chat = ensure_work_item(self.service, 'wi-chat', title='闲聊',
                               acceptance=conversation_acceptance())
        dev = self.service.call('submit_task', dict(
            request_id='acc-dev', agent='fake', prompt='ship', cwd=str(self.root),
            mode='read_only', work_item_id=wid))
        self.assertEqual(dev['verification']['verification_label'], 'not_configured')
        self.service.store.finish(dev['id'], 'succeeded', {'answer': 'done'})
        fields = self.service.verification.fields(self.service.store.get(dev['id']))
        self.assertEqual(fields['verification']['state'], 'awaiting_verification')
        review = self.service.call('submit_task', dict(
            request_id='acc-rev', agent='fake', prompt='review', cwd=str(self.root),
            mode='read_only', work_item_id=wid, relation='review', related_task_id=dev['id']))
        self.assertIsNone(self.service.store.get(review['id']).get('acceptance'))
        hi = self.service.call('submit_task', dict(
            request_id='acc-hi', agent='fake', prompt='hello', cwd=str(self.root),
            mode='read_only', work_item_id=chat))
        self.service.store.finish(hi['id'], 'succeeded', {'answer': 'hi'})
        hi_fields = self.service.verification.fields(self.service.store.get(hi['id']))
        self.assertEqual(hi_fields['verification']['state'], 'not_requested')
        self.assertEqual(hi_fields['verification']['verification_label'], 'completed_no_extra')
        # Bare success without acceptance stays not_configured — never auto-passed.
        bare_wid = ensure_work_item(self.service, 'wi-bare')
        bare = self.service.call('submit_task', dict(
            request_id='acc-bare', agent='fake', prompt='hey', cwd=str(self.root),
            mode='read_only', work_item_id=bare_wid))
        self.service.store.finish(bare['id'], 'succeeded', {'answer': 'ok'})
        bare_fields = self.service.verification.fields(self.service.store.get(bare['id']))
        self.assertEqual(bare_fields['verification']['state'], 'not_requested')
        self.assertEqual(bare_fields['verification']['verification_label'], 'not_configured')
        self.assertNotEqual(bare_fields['verification']['state'], 'passed')
        detail = Dashboard(self.service).detail(bare['id'])
        self.assertEqual(detail['task']['verification_label'], 'not_configured')
        # continue_task acceptance override applies only to child.
        # session/workspace must be set before finish — terminal update is guarded.
        cont_parent = self.service.call('submit_task', dict(
            request_id='acc-cont-p', agent='fake', prompt='ship2', cwd=str(self.root),
            mode='read_only', work_item_id=wid))
        self.service.store.update(cont_parent['id'], session_id='native-acc', workspace=str(self.root))
        self.service.store.finish(cont_parent['id'], 'succeeded', {'answer': 'done'})
        child = self.service.call('continue_task', dict(
            request_id='acc-cont', task_id=cont_parent['id'], prompt='next',
            acceptance=conversation_acceptance()))
        self.assertEqual(self.service.store.get(child['id']).get('acceptance'),
                         conversation_acceptance())
        self.assertEqual(self.service.store.get(cont_parent['id']).get('acceptance'), soft)


class ReconcileTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root / 'data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def _interrupted_grok_like(self, suffix='g1', prompt='review'):
        path = self.service.root / 'results' / (suffix + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'error': '连接中断', 'error_code': 'REMOTE_STATE_UNKNOWN', 'remote_cancelled': False,
        }, ensure_ascii=False), encoding='utf-8')
        # This is historical state, not a queued dispatch. Hold the scheduler's
        # lock across both commits so it cannot launch Grok in the seeding gap.
        with self.service.lock:
            task = self.service.store.create('req-' + suffix, dict(
                agent='grok', prompt=prompt, cwd=str(self.root), mode='delegate',
                timeout_seconds=1800, delivery='accepted', baseline=240, session_id='12535208'))
            self.service.store.finish(task['id'], 'interrupted', {
                'error': '连接中断', 'error_code': 'REMOTE_STATE_UNKNOWN', 'remote_cancelled': False,
                'path': str(path)})
        return self.service.store.get(task['id'])

    def _page(self, cursor='240', entries=None, busy=False):
        return dict(entries=entries or [], latestUpdatedSeq=cursor,
                    turn=dict(idle=not busy, inFlight=busy, queued=0))

    def _install_history_bot(self, pages, *, post_raises=True):
        """Wire real GrokBot.reconcile; only override transport request()."""
        from agent_hub.adapters.grokbot import GrokBot
        pages = list(pages)
        self_outer = self

        class HistoryBot(GrokBot):
            def __init__(self):
                super().__init__('hist-bot', state_file=str(self_outer.root / 'lock.json'))
                self.calls = []
                self._pages = iter(pages)

            def request(self, method, path, body, timeout):
                self.calls.append((method, path, body))
                if method != 'GET':
                    if post_raises:
                        raise AssertionError('reconcile must not POST')
                    return {}
                try:
                    return next(self._pages)
                except StopIteration:
                    return self_outer._page('999')

        bot = HistoryBot()
        self.service.adapters['grok'] = bot
        return bot

    def test_gateway_uncertain_not_definitive_failure(self):
        task = self._interrupted_grok_like('gw')
        result = self.service.call('get_result', {'task_id': task['id']})
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['gateway_state'], 'unknown')
        self.assertTrue(result['execution_uncertain'])
        self.assertTrue(result['errors'])
        self.assertFalse(result['errors'][0]['retryable'])
        self.assertNotEqual(result['gateway_state'], 'failed')

    def test_reconcile_own_prompt_and_marker_completes_with_idle4(self):
        from agent_hub.adapters.grokbot import GrokBot
        task = self._interrupted_grok_like('own', prompt='do review')
        bot_probe = GrokBot('probe', state_file=str(self.root / 'p.json'))
        prompt = bot_probe.dispatched_prompt(task)
        marker = '[HUB-END:' + task['id'] + ']'
        own_user = dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt)
        final = dict(seq='250', updatedSeq='250', kind='send-message', role='assistant',
                     text='done\n' + marker)
        # Marker page must not be busy; then ≥4 stable idle polls (protocol, not shortened).
        pages = [self._page('250', [own_user, final])] + [self._page('250')] * 6
        bot = self._install_history_bot(pages)
        lease = self.service.call('claim_controller', dict(
            request_id='own-claim', task_id=task['id'], controller_id='ops'))
        done = self.service.call('reconcile_task', dict(
            request_id='own-1', task_id=task['id'],
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertTrue(done['completed'])
        self.assertEqual(self.service.store.get(task['id'])['state'], 'succeeded')
        result_files = list((self.service.root / 'results').glob(task['id'] + '.reconciled.*.json'))
        self.assertEqual(len(result_files), 1)
        again = self.service.call('reconcile_task', dict(
            request_id='own-1', task_id=task['id'],
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertEqual(again, done)
        self.assertTrue(all(c[0] == 'GET' for c in bot.calls))

    def test_reconcile_foreign_wrong_marker_idle_network(self):
        from agent_hub.adapters.grokbot import GrokBot
        task = self._interrupted_grok_like('amb', prompt='p')
        bot_probe = GrokBot('probe', state_file=str(self.root / 'p2.json'))
        prompt = bot_probe.dispatched_prompt(task)
        own_user = dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt)
        foreign = dict(seq='242', updatedSeq='242', kind='message', role='user', text='human typed')
        wrong = dict(seq='243', updatedSeq='243', kind='send-message', role='assistant',
                     text='x\n[HUB-END:other]')
        bot = self._install_history_bot([self._page('242', [own_user, foreign])])
        lease = self.service.call('claim_controller', dict(
            request_id='f-claim', task_id=task['id'], controller_id='ops'))
        amb = self.service.call('reconcile_task', dict(
            request_id='f-1', task_id=task['id'],
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertFalse(amb['completed'])
        self.assertEqual(amb['observation']['status'], 'ambiguous')
        self.assertEqual(self.service.store.get(task['id'])['state'], 'interrupted')

        task2 = self._interrupted_grok_like('mk', prompt='p2')
        bot2 = self._install_history_bot([self._page('243', [
            dict(seq='241', updatedSeq='241', kind='message', role='user',
                 text=GrokBot('x', state_file=str(self.root / 'x.json')).dispatched_prompt(task2)),
            wrong])])
        lease2 = self.service.call('claim_controller', dict(
            request_id='mk-claim', task_id=task2['id'], controller_id='ops2'))
        amb2 = self.service.call('reconcile_task', dict(
            request_id='mk-1', task_id=task2['id'],
            controller_id=lease2['controller_id'], controller_epoch=lease2['controller_epoch']))
        self.assertEqual(amb2['observation']['status'], 'ambiguous')

        task3 = self._interrupted_grok_like('idle3', prompt='p3')
        self._install_history_bot([self._page('240')] * 8)
        lease3 = self.service.call('claim_controller', dict(
            request_id='id-claim', task_id=task3['id'], controller_id='ops3'))
        idle = self.service.call('reconcile_task', dict(
            request_id='id-1', task_id=task3['id'],
            controller_id=lease3['controller_id'], controller_epoch=lease3['controller_epoch']))
        self.assertFalse(idle['completed'])
        self.assertEqual(idle['observation']['status'], 'network_unknown')

        task4 = self._interrupted_grok_like('net', prompt='p4')
        from agent_hub.adapters.grokbot import GrokBot as GB

        class NetBot(GB):
            def __init__(self):
                super().__init__('net', state_file=str(self_outer.root / 'n.json'))

            def request(self, method, path, body, timeout):
                raise httpx.ConnectError('boom')

        self_outer = self
        self.service.adapters['grok'] = NetBot()
        lease4 = self.service.call('claim_controller', dict(
            request_id='n-claim', task_id=task4['id'], controller_id='ops4'))
        net = self.service.call('reconcile_task', dict(
            request_id='n-1', task_id=task4['id'],
            controller_id=lease4['controller_id'], controller_epoch=lease4['controller_epoch']))
        self.assertFalse(net['completed'])
        self.assertEqual(net['observation']['status'], 'network_unknown')

    def test_reconcile_does_not_revive_cancelled(self):
        from agent_hub.adapters.grokbot import GrokBot
        task = self._interrupted_grok_like('cancel', prompt='c')
        prompt = GrokBot('p', state_file=str(self.root / 'c.json')).dispatched_prompt(task)
        marker = '[HUB-END:' + task['id'] + ']'
        pages = [self._page('250', [
            dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt),
            dict(seq='250', updatedSeq='250', kind='send-message', role='assistant',
                 text='x\n' + marker),
        ])] + [self._page('250')] * 6
        bot = self._install_history_bot(pages)
        with self.service.store.connection(write=True) as db:
            db.execute("UPDATE tasks SET state='cancelled' WHERE id=?", (task['id'],))
        lease = self.service.call('claim_controller', dict(
            request_id='c-claim', task_id=task['id'], controller_id='ops'))
        result = self.service.call('reconcile_task', dict(
            request_id='c-1', task_id=task['id'],
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertFalse(result['completed'])
        self.assertEqual(self.service.store.get(task['id'])['state'], 'cancelled')
        self.assertTrue(all(c[0] == 'GET' for c in bot.calls))

    def test_reconcile_request_conflict_and_stale_controller(self):
        from agent_hub.adapters.grokbot import GrokBot
        task = self._interrupted_grok_like('rev', prompt='r')
        prompt = GrokBot('p', state_file=str(self.root / 'r.json')).dispatched_prompt(task)
        marker = '[HUB-END:' + task['id'] + ']'
        pages = [self._page('250', [
            dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt),
            dict(seq='250', updatedSeq='250', kind='send-message', role='assistant',
                 text='ok\n' + marker),
        ])] + [self._page('250')] * 6
        self._install_history_bot(pages)
        lease = self.service.call('claim_controller', dict(
            request_id='rv-claim', task_id=task['id'], controller_id='ops'))
        done = self.service.call('reconcile_task', dict(
            request_id='rv-1', task_id=task['id'],
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertTrue(done['completed'])
        other = self._interrupted_grok_like('rev2', prompt='r2')
        lease2 = self.service.call('claim_controller', dict(
            request_id='rv-claim-2', task_id=other['id'], controller_id='ops2'))
        with self.assertRaises(HubError) as err:
            self.service.call('reconcile_task', dict(
                request_id='rv-1', task_id=other['id'],
                controller_id=lease2['controller_id'], controller_epoch=lease2['controller_epoch']))
        self.assertEqual(err.exception.code, 'REQUEST_CONFLICT')

    def test_reconcile_stale_lease_after_network_does_not_complete(self):
        from agent_hub.adapters.grokbot import GrokBot
        task = self._interrupted_grok_like('stale', prompt='s')
        prompt = GrokBot('p', state_file=str(self.root / 's.json')).dispatched_prompt(task)
        marker = '[HUB-END:' + task['id'] + ']'
        pages = [self._page('250', [
            dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt),
            dict(seq='250', updatedSeq='250', kind='send-message', role='assistant',
                 text='ok\n' + marker),
        ])] + [self._page('250')] * 6
        lease = self.service.call('claim_controller', dict(
            request_id='st-claim', task_id=task['id'], controller_id='ops'))
        stolen = {'done': False}
        bot = self._install_history_bot(pages)
        original_request = bot.request

        def steal_then_get(method, path, body, timeout):
            if not stolen['done']:
                stolen['done'] = True
                # Mid-flight takeover while reconcile network is outside Service.lock.
                self.service.call('claim_controller', dict(
                    request_id='st-claim-2', task_id=task['id'], controller_id='ops-new'))
            return original_request(method, path, body, timeout)

        bot.request = steal_then_get
        with self.assertRaises(HubError) as err:
            self.service.call('reconcile_task', dict(
                request_id='st-1', task_id=task['id'],
                controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertEqual(err.exception.code, 'CONTROLLER_STALE')
        self.assertEqual(self.service.store.get(task['id'])['state'], 'interrupted')
        self.assertFalse(list((self.service.root / 'results').glob(task['id'] + '.reconciled.*.json')))

    def test_reconcile_replace_fail_evil_path_progress_resume_and_slow_get(self):
        from unittest.mock import patch
        from agent_hub.adapters.grokbot import GrokBot
        import time as time_mod

        def pages_for(task, prompt_text, marker_text):
            return [self._page('250', [
                dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt_text),
                dict(seq='250', updatedSeq='250', kind='send-message', role='assistant',
                     text='ok\n' + marker_text),
            ])] + [self._page('250')] * 6

        # 1) os.replace failure must not complete or leave succeeded→missing file.
        task = self._interrupted_grok_like('rep', prompt='replace-me')
        prompt = GrokBot('p', state_file=str(self.root / 'rep.json')).dispatched_prompt(task)
        marker = '[HUB-END:' + task['id'] + ']'
        pages = pages_for(task, prompt, marker)
        self._install_history_bot(pages)
        lease = self.service.call('claim_controller', dict(
            request_id='rep-claim', task_id=task['id'], controller_id='ops'))
        with patch('os.replace', side_effect=OSError('disk full')):
            failed = self.service.call('reconcile_task', dict(
                request_id='rep-1', task_id=task['id'],
                controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertFalse(failed['completed'])
        self.assertEqual(self.service.store.get(task['id'])['state'], 'interrupted')
        self.assertFalse(list((self.service.root / 'results').glob(task['id'] + '.reconciled.*.json')))
        self._install_history_bot(pages)
        done = self.service.call('reconcile_task', dict(
            request_id='rep-2', task_id=task['id'],
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertTrue(done['completed'])
        final = list((self.service.root / 'results').glob(task['id'] + '.reconciled.*.json'))
        self.assertEqual(len(final), 1)
        self.assertTrue(final[0].is_file())
        self.assertTrue(json.loads(final[0].read_text(encoding='utf-8'))['answer'].rstrip().endswith(marker))

        # 2) Evil request_id must never create nested result paths.
        task2 = self._interrupted_grok_like('evil', prompt='evil-path')
        prompt2 = GrokBot('p2', state_file=str(self.root / 'e.json')).dispatched_prompt(task2)
        marker2 = '[HUB-END:' + task2['id'] + ']'
        self._install_history_bot(pages_for(task2, prompt2, marker2))
        lease2 = self.service.call('claim_controller', dict(
            request_id='evil-claim', task_id=task2['id'], controller_id='ops2'))
        evil_rid = '../evil:req\\..\\nest'
        done2 = self.service.call('reconcile_task', dict(
            request_id=evil_rid, task_id=task2['id'],
            controller_id=lease2['controller_id'], controller_epoch=lease2['controller_epoch']))
        self.assertTrue(done2['completed'])
        results_root = (self.service.root / 'results').resolve()
        written = list(results_root.glob(task2['id'] + '.reconciled.*.json'))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].resolve().parent, results_root)
        self.assertNotIn('..', written[0].name)
        self.assertNotIn(':', written[0].name)
        self.assertNotIn('\\', written[0].name)

        # 3) Progress resumes across calls after a short probe sees the marker.
        task3 = self._interrupted_grok_like('prog', prompt='progress')
        prompt3 = GrokBot('p3', state_file=str(self.root / 'pr.json')).dispatched_prompt(task3)
        marker3 = '[HUB-END:' + task3['id'] + ']'
        first_pages = [self._page('250', [
            dict(seq='241', updatedSeq='241', kind='message', role='user', text=prompt3),
            dict(seq='250', updatedSeq='250', kind='send-message', role='assistant',
                 text='mid\n' + marker3),
        ])] + [self._page('250')] * 6
        bot = self._install_history_bot(first_pages)
        lease3 = self.service.call('claim_controller', dict(
            request_id='prog-claim', task_id=task3['id'], controller_id='ops3'))
        real_reconcile = bot.reconcile

        def short_reconcile(task_arg, *, timeout_seconds=24):
            return real_reconcile(task_arg, timeout_seconds=1.0)

        bot.reconcile = short_reconcile
        partial = self.service.call('reconcile_task', dict(
            request_id='prog-1', task_id=task3['id'],
            controller_id=lease3['controller_id'], controller_epoch=lease3['controller_epoch']))
        self.assertFalse(partial['completed'])
        progress = self.service.store.get(task3['id']).get('reconcile_progress')
        self.assertIsInstance(progress, dict)
        self.assertTrue(progress.get('own_marker_seen'))
        self.assertTrue(progress.get('own_message_observed'))
        self.assertGreaterEqual(progress.get('idle_count', 0), 1)
        self.assertEqual(progress.get('session_id'), '12535208')
        self.assertEqual(progress.get('baseline'), 240)
        self.assertEqual(progress.get('marker'), marker3)
        # Foreign/mismatched progress must not forge completion on another task.
        other = self._interrupted_grok_like('prog-other', prompt='other')
        stolen = dict(progress, session_id='other-session', marker='[HUB-END:' + other['id'] + ']')
        with self.service.store.connection(write=True) as db:
            row = db.execute('SELECT data FROM tasks WHERE id=?', (other['id'],)).fetchone()
            data = json.loads(row['data'])
            data['reconcile_progress'] = stolen
            db.execute('UPDATE tasks SET data=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), other['id']))
        self._install_history_bot([self._page('240')] * 3)
        lease_other = self.service.call('claim_controller', dict(
            request_id='prog-other-claim', task_id=other['id'], controller_id='ops-other'))
        refused = self.service.call('reconcile_task', dict(
            request_id='prog-other-1', task_id=other['id'],
            controller_id=lease_other['controller_id'],
            controller_epoch=lease_other['controller_epoch']))
        self.assertFalse(refused['completed'])
        self.assertEqual(self.service.store.get(other['id'])['state'], 'interrupted')

        # Second call resumes idle from persisted progress and completes.
        resume_bot = self._install_history_bot([self._page('250')] * 6)
        done3 = self.service.call('reconcile_task', dict(
            request_id='prog-2', task_id=task3['id'],
            controller_id=lease3['controller_id'], controller_epoch=lease3['controller_epoch']))
        self.assertTrue(done3['completed'])
        self.assertEqual(self.service.store.get(task3['id'])['state'], 'succeeded')
        self.assertIsNone(self.service.store.get(task3['id']).get('reconcile_progress'))
        self.assertTrue(all(c[0] == 'GET' for c in resume_bot.calls))

        # 4) Slow GET still completes within the bounded 24s probe.
        task4 = self._interrupted_grok_like('slow', prompt='slow-get')
        prompt4 = GrokBot('p4', state_file=str(self.root / 'sl.json')).dispatched_prompt(task4)
        marker4 = '[HUB-END:' + task4['id'] + ']'
        slow_bot = self._install_history_bot(pages_for(task4, prompt4, marker4))
        original_request = slow_bot.request

        def slow_request(method, path, body, timeout):
            time_mod.sleep(0.35)
            return original_request(method, path, body, timeout)

        slow_bot.request = slow_request
        lease4 = self.service.call('claim_controller', dict(
            request_id='slow-claim', task_id=task4['id'], controller_id='ops4'))
        started = time_mod.monotonic()
        done4 = self.service.call('reconcile_task', dict(
            request_id='slow-1', task_id=task4['id'],
            controller_id=lease4['controller_id'], controller_epoch=lease4['controller_epoch']))
        self.assertTrue(done4['completed'])
        self.assertLess(time_mod.monotonic() - started, 24)
        self.assertTrue(all(c[0] == 'GET' for c in slow_bot.calls))
        # Client HTTP timeout must exceed service probe bound (documented pairing).
        self.assertGreater(45, 24)


class RedactionAndBudgetTests(unittest.TestCase):
    def test_command_display_redacts_spaced_and_quoted_secrets(self):
        from agent_hub.models import redact
        samples = [
            'example --password synthetic-not-a-secret',
            "tool --password 'quoted-value'",
            'tool --token="tok"',
            'Authorization: Bearer abc.def.ghi',
            'https://user:pass@example.com/x',
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                out = redact(sample)
                self.assertNotIn('synthetic-not-a-secret', out)
                self.assertNotIn('quoted-value', out)
                self.assertNotIn('abc.def.ghi', out)
                self.assertNotIn('user:pass', out)

    def test_provider_revision_dedup_keeps_highest_stable_id(self):
        from agent_hub.dashboard_timeline import dedupe_provider_messages
        entries = [
            dict(id='a', task_id='t', kind='message', provider_message_id='9', provider_revision=1, text='v1'),
            dict(id='b', task_id='t', kind='message', provider_message_id='9', provider_revision=3, text='v3'),
            dict(id='c', task_id='t', kind='message', provider_message_id='9', provider_revision=2, text='v2'),
        ]
        out = dedupe_provider_messages(entries)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['text'], 'v3')
        self.assertEqual(out[0]['id'], 't:provider:9')


if __name__ == '__main__':
    unittest.main()
