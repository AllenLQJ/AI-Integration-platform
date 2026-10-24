"""Acceptance policy labels + software_dashboard external verification (no parallel state machine)."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_hub.models import HubError
from agent_hub.service import Service
from agent_hub.work_items import conversation_acceptance, software_dashboard_acceptance
from hub_fixtures import ensure_work_item
from test_service import Fake


class AcceptanceDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root / 'data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def _artifact(self, task_id, name, body=b'ok'):
        path = self.root / name
        path.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        task = self.service.store.get(task_id)
        return self.service.verification.index(task, 'external', name, dict(
            sha256=digest, size_bytes=len(body), path=str(path), mime_type='text/plain'))

    def test_partial_and_full_software_dashboard_external(self):
        wid = ensure_work_item(self.service, 'wi-soft', acceptance=software_dashboard_acceptance())
        task = self.service.call('submit_task', dict(
            request_id='soft-1', agent='fake', prompt='implement dashboard',
            cwd=str(self.root), mode='read_only', work_item_id=wid))
        self.service.store.finish(task['id'], 'succeeded', {'answer': 'done'})
        self.service.verification.ensure(self.service.store.get(task['id']))
        art = self._artifact(task['id'], 'evidence-tests.txt', b'tests pass')
        partial = self.service.call('record_external_verification', dict(
            request_id='ext-partial', task_id=task['id'], verifier_id='controller-ops',
            checks=[dict(criterion_id='tests', outcome='pass', reason='unittest ok',
                         evidence=[art['artifact_id']])]))
        self.assertNotEqual(partial['verification']['state'], 'passed')
        art_ui = self._artifact(task['id'], 'evidence-ui.txt', b'ui ok')
        art_rev = self._artifact(task['id'], 'evidence-rev.txt', b'review ok')
        failed_review = self.service.call('record_external_verification', dict(
            request_id='ext-fail-rev', task_id=task['id'], verifier_id='controller-ops',
            checks=[
                dict(criterion_id='tests', outcome='pass', reason='ok', evidence=[art['artifact_id']]),
                dict(criterion_id='ui', outcome='pass', reason='ok', evidence=[art_ui['artifact_id']]),
                dict(criterion_id='review', outcome='fail', reason='grok found issues',
                     evidence=[art_rev['artifact_id']]),
            ]))
        self.assertEqual(failed_review['verification']['state'], 'rework_required')
        # Terminal store.update is rejected; set session before finish on a fresh attempt.
        parent = self.service.call('submit_task', dict(
            request_id='soft-1b', agent='fake', prompt='implement dashboard v2',
            cwd=str(self.root), mode='read_only', work_item_id=wid))
        self.service.store.update(parent['id'], session_id='s1', workspace=str(self.root))
        self.service.store.finish(parent['id'], 'succeeded', {'answer': 'done'})
        self.service.verification.ensure(self.service.store.get(parent['id']))
        child = self.service.call('continue_task', dict(
            request_id='soft-2', task_id=parent['id'], prompt='fix'))
        self.service.store.update(child['id'], session_id='s2', workspace=str(self.root))
        self.service.store.finish(child['id'], 'succeeded', {'answer': 'fixed'})
        self.service.verification.ensure(self.service.store.get(child['id']))
        arts = [self._artifact(child['id'], 'c-%s.txt' % i, bytes([i])) for i in range(3)]
        passed = self.service.call('record_external_verification', dict(
            request_id='ext-pass', task_id=child['id'], verifier_id='controller-ops',
            checks=[
                dict(criterion_id='tests', outcome='pass', reason='ok', evidence=[arts[0]['artifact_id']]),
                dict(criterion_id='ui', outcome='pass', reason='ok', evidence=[arts[1]['artifact_id']]),
                dict(criterion_id='review', outcome='pass', reason='grok approved',
                     evidence=[arts[2]['artifact_id']]),
            ]))
        self.assertEqual(passed['verification']['state'], 'passed')
        stale = self.service.call('record_external_verification', dict(
            request_id='ext-stale', task_id=parent['id'], verifier_id='controller-ops',
            checks=[dict(criterion_id='tests', outcome='pass', reason='late',
                         evidence=[self._artifact(parent['id'], 'late.txt', b'late')['artifact_id']])]))
        self.assertTrue(stale.get('superseded'))
        self.assertEqual(
            self.service.verification.fields(self.service.store.get(child['id']))['verification']['state'],
            'passed')

    def test_conversation_no_extra_and_hub_verifier_rejected(self):
        wid = ensure_work_item(self.service, 'wi-chat2', acceptance=conversation_acceptance())
        task = self.service.call('submit_task', dict(
            request_id='chat-1', agent='fake', prompt='hi', cwd=str(self.root),
            mode='read_only', work_item_id=wid))
        self.service.store.finish(task['id'], 'succeeded', {'answer': 'hello'})
        fields = self.service.verification.fields(self.service.store.get(task['id']))
        self.assertEqual(fields['verification']['verification_label'], 'completed_no_extra')
        self.assertEqual(fields['verification']['state'], 'not_requested')
        with self.assertRaises(HubError) as err:
            self.service.call('record_external_verification', dict(
                request_id='bad-v', task_id=task['id'], verifier_id='hub-native',
                checks=[]))
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')


if __name__ == '__main__':
    unittest.main()
