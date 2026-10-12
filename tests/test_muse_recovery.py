import asyncio
import json
import threading
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub.adapters.muse import Muse
from agent_hub.models import HubError


class MuseRecoveryTests(unittest.TestCase):
    def execute(self, responses, deadline=None, cancel=None):
        calls, events, arguments = [], [], []
        class Session:
            async def initialize(self): pass
            async def call_tool(self, name, args, **kwargs):
                calls.append(name)
                arguments.append((name, args))
                value = responses[name].pop(0)
                if callable(value): value = value()
                if isinstance(value, Exception): raise value
                return SimpleNamespace(isError=False, structuredContent={'result': value})
        @asynccontextmanager
        async def transport(*args):
            self.transport_env = args[0].env
            yield None, None
        @asynccontextmanager
        async def session(*args): yield Session()
        self.calls, self.events, self.arguments = calls, events, arguments
        with patch('mcp.client.stdio.stdio_client', transport), patch('mcp.ClientSession', session):
            return asyncio.run(Muse('unused').execute(
                dict(id='recovery', prompt='verify', deadline=deadline or time.time()+15),
                cancel or threading.Event(), lambda *args: events.append(args)))

    @staticmethod
    def error(code='GATEWAY_ERROR'):
        return {'ok': False, 'error': {'code': code, 'message': 'safe gateway failure'}}

    @staticmethod
    def reply():
        return [{'seq': 9, 'role': 'assistant', 'reply_to_message_id': 'user-message',
                 'text': 'done https://example.org/result\n[HUB:recovery]',
                 'raw': {'payload': {'status': 'completed', 'display_text_ready': True}}}]

    def test_creation_inner_error_keeps_stage_and_code_without_retry(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [self.error()]})
        self.assertEqual(caught.exception.code, 'GATEWAY_ERROR')
        self.assertIn('session.start', str(caught.exception))
        self.assertEqual(self.calls, ['muse_raw'])

    def test_dedicated_worker_enables_serialized_gateway_reuse(self):
        with self.assertRaises(HubError):
            self.execute({'muse_raw': [self.error()]})
        self.assertEqual(self.transport_env.get('MUSE_MCP_REUSE_GATEWAY'), '1')

    def test_baseline_invalid_history_prevents_send(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}], 'muse_history': [{}]})
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.assertNotIn('muse_send', self.calls)

    def test_transient_history_error_recovers_nonempty_reply(self):
        result = self.execute({'muse_raw': [{'session_id': 'sid'}],
            'muse_send': [{'sent': True}],
            'muse_history': [[], self.error(), self.reply(), self.reply()]})
        self.assertEqual(result['completion_evidence']['reply_to_message_id'], 'user-message')
        self.assertEqual(result['completion_evidence']['stable_polls'], 2)
        self.assertEqual(result['source_urls'], ['https://example.org/result'])
        self.assertEqual(self.calls.count('muse_send'), 1)
        self.assertIn('retry', [kind for kind, data in self.events])

    def test_exhausted_reads_preserve_remote_uncertainty(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}], 'muse_send': [{'sent': True}],
                'muse_history': [[], self.error(), self.error(), self.error()]})
        self.assertEqual(caught.exception.code, 'REMOTE_STATE_UNKNOWN')
        self.assertIn('GATEWAY_ERROR', str(caught.exception))
        self.assertEqual(self.calls.count('muse_history'), 4)
        self.assertEqual(self.calls.count('muse_send'), 1)

    def test_auth_error_is_not_retried(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}],
                'muse_history': [self.error('AUTH_REQUIRED')]})
        self.assertEqual(caught.exception.code, 'AUTH_REQUIRED')
        self.assertEqual(self.calls.count('muse_history'), 1)

    def test_send_inner_error_is_uncertain_and_never_resent(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}], 'muse_history': [[]],
                'muse_send': [self.error()]})
        self.assertEqual(caught.exception.code, 'DELIVERY_UNKNOWN')
        self.assertEqual(self.calls.count('muse_send'), 1)

    def test_send_error_preserves_safe_upstream_diagnostics(self):
        error = self.error()
        error['error'].update(status=503, exception_type='GatewayError',
                              stage='send.preflight_history', dispatch_attempted=False)
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}], 'muse_history': [[]],
                          'muse_send': [error]})
        self.assertEqual(caught.exception.details['status'], 503)
        self.assertEqual(caught.exception.details['muse_stage'], 'send.preflight_history')
        self.assertFalse(caught.exception.details['dispatch_attempted'])
        self.assertEqual(self.calls.count('muse_send'), 1)

    def test_invalid_history_after_send_reports_remote_uncertainty(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}], 'muse_send': [{'sent': True}],
                'muse_history': [[], {'unexpected': 'shape'}]})
        self.assertEqual(caught.exception.code, 'REMOTE_STATE_UNKNOWN')
        self.assertEqual(caught.exception.details['upstream_code'], 'INVALID_RESPONSE')
        self.assertEqual(self.calls.count('muse_history'), 2)

    def test_baseline_connection_error_recovers_before_single_send(self):
        result = self.execute({'muse_raw': [{'session_id': 'sid'}],
            'muse_send': [{'sent': True}],
            'muse_history': [OSError('disconnected'), [], self.reply(), self.reply()]})
        self.assertEqual(result['completion_evidence']['baseline'], 0)
        self.assertEqual(self.calls.count('muse_send'), 1)

    def test_deadline_stops_retry_without_another_read(self):
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}],
                'muse_history': [self.error()]}, deadline=time.time()+.5)
        self.assertEqual(caught.exception.code, 'TIMED_OUT')
        self.assertEqual(self.calls.count('muse_history'), 1)
        self.assertNotIn('muse_send', self.calls)

    def test_cancel_during_retry_stops_without_another_read(self):
        cancel = threading.Event()
        def fail_and_cancel():
            cancel.set()
            return self.error()
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}],
                'muse_history': [fail_and_cancel]}, cancel=cancel)
        self.assertEqual(caught.exception.code, 'CANCELLED')
        self.assertEqual(self.calls.count('muse_history'), 1)
        self.assertNotIn('muse_send', self.calls)

    def test_inner_error_message_is_redacted(self):
        error = self.error()
        error['error']['message'] = 'Authorization: Bearer private-value'
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [error]})
        self.assertNotIn('private-value', str(caught.exception))

    def test_reply_disappearance_requires_two_new_stable_reads(self):
        result = self.execute({'muse_raw': [{'session_id': 'sid'}],
            'muse_send': [{'sent': True}],
            'muse_history': [[], self.reply(), [], self.reply(), self.reply()]})
        self.assertEqual(self.calls.count('muse_history'), 5)
        self.assertEqual(result['completion_evidence']['reply_seq'], 9)

    def test_worker_preserves_error_diagnostics(self):
        from agent_hub.adapters import muse_worker
        class Adapter:
            async def execute(self, *args):
                raise HubError('GATEWAY_ERROR', 'safe failure', {'stage': 'session.start'})
            unwrap = staticmethod(lambda error: error)
        data = dict(executable='unused', args=[], env={}, task={})
        with patch.object(muse_worker, 'Muse', return_value=Adapter()), \
             patch('pathlib.Path.read_text', return_value=json.dumps(data)), \
             patch('sys.argv', ['worker', 'unused']), patch('builtins.print') as output:
            muse_worker.main()
        result = json.loads(output.call_args.args[0])['hub_result']
        self.assertEqual(result['stage'], 'session.start')

    def test_parent_preserves_worker_error_diagnostics(self):
        import tempfile
        payload = {'hub_result': {'error_code': 'GATEWAY_ERROR', 'error': 'safe failure',
                                  'stage': 'session.start', 'upstream_code': 'GATEWAY_ERROR'}}
        with tempfile.TemporaryDirectory() as directory, \
             patch('agent_hub.adapters.muse.run_process',
                   return_value=(0, json.dumps(payload), '', {})):
            with self.assertRaises(HubError) as caught:
                Muse('unused').run(dict(log_dir=directory, workspace=directory,
                                       deadline=time.time()+10), threading.Event(), lambda *args: None)
        self.assertEqual(caught.exception.details['stage'], 'session.start')
        self.assertEqual(caught.exception.details['upstream_code'], 'GATEWAY_ERROR')

    def test_sdk_timeout_is_retried_and_recovers(self):
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData
        result = self.execute({'muse_raw': [{'session_id': 'sid'}],
            'muse_send': [{'sent': True}],
            'muse_history': [[], McpError(ErrorData(code=408, message='RPC timeout')),
                             self.reply(), self.reply()]})
        self.assertEqual(result['completion_evidence']['reply_seq'], 9)
        self.assertEqual(self.events[2][1]['error_code'], 'TIMEOUT')

    def test_sdk_protocol_error_is_not_retried(self):
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}],
                'muse_history': [McpError(ErrorData(code=-32602, message='invalid params'))]})
        self.assertEqual(caught.exception.code, 'MUSE_ERROR')
        self.assertEqual(self.calls.count('muse_history'), 1)

    def test_sdk_send_timeout_keeps_rpc_code_without_resend(self):
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}], 'muse_history': [[]],
                'muse_send': [McpError(ErrorData(code=408, message='RPC timeout'))]})
        self.assertEqual(caught.exception.code, 'DELIVERY_UNKNOWN')
        self.assertEqual(caught.exception.details['rpc_code'], 408)
        self.assertEqual(self.calls.count('muse_send'), 1)

    def test_dispatched_send_with_reply_wait_timeout_is_collected(self):
        sent = {'ok': False, 'sent': True, 'acknowledged': False,
                'error': {'code': 'TIMEOUT', 'message': 'dispatched but reply wait expired'}}
        result = self.execute({'muse_raw': [{'session_id': 'sid'}],
            'muse_send': [sent], 'muse_history': [[], self.reply(), self.reply()]})
        self.assertEqual(result['completion_evidence']['reply_seq'], 9)
        self.assertEqual(self.calls.count('muse_send'), 1)
        self.assertIn('accepted', [kind for kind, data in self.events])

    def test_send_returns_after_dispatch_and_hub_collects_reply(self):
        result = self.execute({'muse_raw': [{'session_id': 'sid'}],
            'muse_send': [{'ok': True, 'sent': True, 'waiting': False}],
            'muse_history': [[], self.reply(), self.reply()]})
        send = next(args for name, args in self.arguments if name == 'muse_send')
        self.assertEqual(send['wait_seconds'], 0)
        self.assertIs(send['fire_and_forget'], True)
        self.assertEqual(result['completion_evidence']['stable_polls'], 2)
        self.assertEqual(self.calls.count('muse_send'), 1)

    def test_confirmed_send_other_business_error_is_not_accepted(self):
        sent = dict(self.error(), sent=True)
        with self.assertRaises(HubError) as caught:
            self.execute({'muse_raw': [{'session_id': 'sid'}],
                'muse_send': [sent], 'muse_history': [[]]})
        self.assertEqual(caught.exception.code, 'DELIVERY_UNKNOWN')
        self.assertEqual(self.calls.count('muse_send'), 1)


if __name__ == '__main__': unittest.main()
