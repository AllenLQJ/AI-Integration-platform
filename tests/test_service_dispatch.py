"""Characterization of Service.call routing and argument validation.

These pin the observable behaviour (which tool needs which arguments, which error
code comes back) so the dispatcher can be restructured without changing it.
"""
import tempfile
import unittest
from pathlib import Path
from agent_hub.models import HubError
from agent_hub.service import Service
from test_service import Fake

CONTROLLED = ('continue_task', 'cancel_task', 'verify_task', 'record_verification', 'switch_executor',
              'record_external_verification', 'record_activity', 'link_task', 'reconcile_task', 'claim_controller')


class ServiceDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def code(self, tool, arguments):
        with self.assertRaises(HubError) as error:
            self.service.call(tool, arguments)
        return error.exception.code

    def finished(self, request_id='dispatch'):
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(
            request_id=request_id, agent='fake', prompt='hi', cwd=str(self.root), mode='read_only')))
        for _ in range(200):
            if self.service.call('get_result', {'task_id': task['id']})['ready']:
                return task['id']
            import time; time.sleep(.02)
        self.fail('task did not finish')

    def test_every_declared_tool_is_routed(self):
        from agent_hub.__main__ import TOOLS
        for tool in TOOLS:
            with self.subTest(tool=tool):
                try:
                    self.service.call(tool, {})
                except HubError as error:
                    self.assertNotEqual(error.code, 'UNKNOWN_TOOL')
                except (TypeError, KeyError):
                    pass  # reached the handler, which wanted arguments

    def test_unknown_tool_is_rejected(self):
        self.assertEqual(self.code('no_such_tool', {}), 'UNKNOWN_TOOL')

    def test_read_only_tools_without_task_arguments(self):
        self.assertEqual(self.service.call('list_agents', {}), {'fake': {'available': True, 'capabilities': Fake.capabilities}})
        self.assertIsInstance(self.service.call('get_capabilities', {}), dict)
        self.assertIsInstance(self.service.call('list_nodes', {}), (dict, list))

    def test_controlled_tools_need_a_task_id_and_reject_internal_fields(self):
        for tool in CONTROLLED:
            with self.subTest(tool=tool, case='missing task_id'):
                self.assertEqual(self.code(tool, {}), 'INVALID_ARGUMENT')
            with self.subTest(tool=tool, case='internal field'):
                self.assertEqual(self.code(tool, {'task_id': 'x', '_internal': 1}), 'INVALID_ARGUMENT')
            with self.subTest(tool=tool, case='unknown task'):
                self.assertEqual(self.code(tool, {'task_id': 'missing', 'request_id': 'r'}), 'TASK_NOT_FOUND')

    def test_per_tool_argument_whitelists(self):
        ident = self.finished()
        cases = {
            'reconcile_task': {'task_id': ident, 'request_id': 'r', 'extra': 1},
            'record_activity': {'task_id': ident, 'request_id': 'r', 'extra': 1},
            'link_task': {'task_id': ident, 'request_id': 'r', 'extra': 1},
            'wait_for_task': {'task_id': ident, 'extra': 1},
            'get_artifact': {'artifact_id': 'a', 'extra': 1},
            'create_work_item': {'request_id': 'r', 'title': 't', 'extra': 1},
            'submit_task': {'request_id': 'r', 'agent': 'fake', 'prompt': 'p', 'cwd': str(self.root),
                            'mode': 'read_only', 'work_item_id': 'w', 'extra': 1},
        }
        for tool, arguments in cases.items():
            with self.subTest(tool=tool):
                self.assertEqual(self.code(tool, arguments), 'INVALID_ARGUMENT')

    def test_wait_for_task_validates_before_waiting(self):
        for arguments in ({}, {'task_id': ''}, {'task_id': 'x', 'after_seq': -1}, {'task_id': 'x', 'timeout_seconds': 11},
                          {'task_id': 'x', 'timeout_seconds': float('nan')}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.code('wait_for_task', arguments), 'INVALID_ARGUMENT')
        self.assertEqual(self.code('wait_for_task', {'task_id': 'missing', 'timeout_seconds': 0}), 'TASK_NOT_FOUND')

    def test_public_submit_requires_a_work_item(self):
        self.assertEqual(self.code('submit_task', dict(request_id='r', agent='fake', prompt='p',
                                                       cwd=str(self.root), mode='read_only')), 'WORK_ITEM_REQUIRED')

    def test_auto_review_request_ids_are_reserved(self):
        for tool in ('submit_task', 'continue_task', 'switch_executor'):
            with self.subTest(tool=tool):
                self.assertEqual(self.code(tool, {'request_id': 'auto-review:x', 'task_id': 'x'}), 'INVALID_ARGUMENT')

    def test_executors_cannot_record_verification_results(self):
        ident = self.finished()
        for tool in ('record_verification', 'record_external_verification'):
            for verifier in ('fake', 'hub-anything'):
                with self.subTest(tool=tool, verifier=verifier):
                    self.assertEqual(self.code(tool, {'task_id': ident, 'request_id': 'r', 'verifier_id': verifier}),
                                     'INVALID_ARGUMENT')

    def test_cursor_bridge_tools_need_the_cursor_adapter(self):
        for tool in ('ide_next_task', 'ide_task_update', 'ide_task_status'):
            with self.subTest(tool=tool):
                self.assertEqual(self.code(tool, {}), 'AGENT_UNAVAILABLE')

    def test_task_reads(self):
        ident = self.finished()
        self.assertEqual(self.service.call('get_task', {'task_id': ident})['id'], ident)
        result = self.service.call('get_result', {'task_id': ident})
        self.assertTrue(result['ready'])
        self.assertEqual(self.service.call('get_work_item', {'task_id': ident})['current_task_id'], ident)
        self.assertEqual(self.service.call('get_events', {'task_id': ident})['contract_version'], '1.0')
        for tool in ('get_task', 'get_result', 'get_work_item', 'get_events'):
            with self.subTest(tool=tool):
                self.assertEqual(self.code(tool, {'task_id': 'missing'}), 'TASK_NOT_FOUND')

    def test_cancel_and_continue_route_through_the_controller_path(self):
        ident = self.finished()
        cancelled = self.service.call('cancel_task', {'task_id': ident})
        self.assertEqual((cancelled['task_id'], cancelled['state']), (ident, 'succeeded'))
        child = self.service.call('continue_task', {'request_id': 'c', 'task_id': ident, 'prompt': 'again'})
        self.assertEqual(child['parent_id'], ident)

    def test_activity_is_recorded_and_replayed(self):
        ident = self.finished()
        recorded = self.service.call('record_activity', {'request_id': 'act', 'task_id': ident, 'event_id': 'e1',
                                                         'activity': {'kind': 'message', 'text': 'noted'}})
        self.assertTrue(recorded['ok'])
        self.assertEqual(recorded['event_id'], 'e1')
        self.assertEqual(self.service.call('record_activity', {'request_id': 'act', 'task_id': ident, 'event_id': 'e1',
                                                               'activity': {'kind': 'message', 'text': 'noted'}}), recorded)


if __name__ == '__main__':
    unittest.main()
