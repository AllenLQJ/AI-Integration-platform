import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub.adapters.muse_wait import HumanWait, find_intervention
from agent_hub.models import HubError
from agent_hub.service import Service
from agent_hub.store import Store
import test_muse_recovery as recovery


def message(seq, text):
    return {'seq': seq, 'role': 'assistant', 'text': text,
            'raw': {'payload': {'status': 'completed', 'display_text_ready': True}}}


class DetectionTests(unittest.TestCase):
    def test_screenshot_message_and_explicit_protocol(self):
        text = '打开设置后遇到了阻碍：人机验证，必须先过这一关。请接管浏览器手动处理。'
        item = find_intervention([message(2, text)], 0, 't')
        self.assertEqual(item['reason'], 'human_verification')
        self.assertEqual(item['evidence'], 'assistant_message_inference')
        for reason in ('login', 'approval', 'human_verification'):
            item = find_intervention([message(3, f'请处理 [HUB_WAIT:t:{reason}]')], 0, 't')
            self.assertEqual(item['reason'], reason)
            self.assertEqual(item['evidence'], 'explicit_message_protocol')

    def test_old_user_generic_and_newer_completed_messages_do_not_block(self):
        wait = message(2, '请处理 [HUB_WAIT:t:login]')
        for rows, baseline in (([wait], 2), ([dict(wait, role='user')], 0),
                ([message(3, '文档介绍人机验证，可以手动操作')], 0),
                ([wait, message(4, '已查到信息 [HUB:t]')], 0)):
            self.assertIsNone(find_intervention(rows, baseline, 't'))

    def test_wait_budget_excludes_execution_and_duplicate_messages(self):
        task = dict(id='t', deadline=110)
        events = []
        with patch('agent_hub.adapters.muse_wait.time.time', return_value=100):
            tracker = HumanWait(task, 'sid', lambda *v: events.append(v))
            tracker.observe([message(1, '[HUB_WAIT:t:login]')], 0)
            tracker.observe([message(1, '[HUB_WAIT:t:login]')], 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(task['deadline'], 1000)
        with patch('agent_hub.adapters.muse_wait.time.time', return_value=120):
            tracker.resume()
            tracker.observe([message(1, '[HUB_WAIT:t:login]')], 0)
        self.assertEqual(task['deadline'], 130)
        self.assertIsNone(tracker.current)
        with patch('agent_hub.adapters.muse_wait.time.time', return_value=125):
            tracker.observe([message(2, '[HUB_WAIT:t:approval]')], 0)
        self.assertEqual(task['deadline'], 1005)  # 900 秒总人工预算减去已用 20 秒


class ServiceWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.release = threading.Event()
        outer = self
        class Adapter:
            capabilities = ['delegate', 'continue']
            remote_cancel = True
            cancel_scope = 'remote_session'
            def run(self, task, cancel, event):
                outer.task = task
                Path(task['log_dir']).mkdir(parents=True, exist_ok=True)
                event('session', {'session_id': 'sid'})
                event('waiting_for_user', dict(intervention_id='wait1', session_id='sid',
                      wait_deadline=time.time()+30, execution_deadline=time.time()+30))
                while not outer.release.wait(.01):
                    if cancel.is_set():
                        raise HubError('CANCELLED', '取消已确认', {'remote_cancelled': True})
                event('user_wait_resumed', dict(execution_deadline=time.time()+10))
                return dict(answer='done', session_id='sid')
        self.service = Service(self.tmp.name, {'muse': Adapter()})
        from hub_fixtures import with_work_item
        self.task_id = self.service.call('submit_task', with_work_item(self.service, dict(
            request_id='start', agent='muse', prompt='lookup', cwd=self.tmp.name, mode='delegate')))['id']
        for _ in range(200):
            if self.service.store.get(self.task_id)['state'] == 'waiting_for_user':
                break
            time.sleep(.01)
        self.assertEqual(self.service.store.get(self.task_id)['state'], 'waiting_for_user')
    def tearDown(self):
        self.release.set()
        self.service.close()
        self.tmp.cleanup()
    def test_wait_returns_immediately_and_result_is_not_ready(self):
        start = time.monotonic()
        snapshot = self.service.wait_for_task(self.task_id, timeout_seconds=10)
        self.assertLess(time.monotonic()-start, .2)
        self.assertTrue(snapshot['needs_user'])
        self.assertFalse(snapshot['timed_out'])
        result = self.service.call('get_result', {'task_id': self.task_id})
        self.assertFalse(result['ready'])
        self.assertEqual(result['intervention']['session_id'], 'sid')
        self.assertEqual(result['gateway_state'], 'running')
    def test_resume_is_same_task_and_duplicate_requests_do_not_rewrite_command(self):
        args = dict(request_id='r', task_id=self.task_id, intervention_id='wait1')
        self.service.call('resume_task', args)
        path = Path(self.tmp.name, 'logs', self.task_id, 'muse-resume.json')
        self.assertTrue(path.exists())
        path.unlink()  # worker 已消耗指令
        self.service.call('resume_task', args)
        self.service.call('resume_task', dict(args, request_id='another'))
        self.assertFalse(path.exists())
        self.assertEqual(len(self.service.store.list()), 1)
        with self.assertRaises(HubError):
            self.service.call('resume_task', dict(args, intervention_id='wrong'))
        with self.assertRaises(HubError):
            self.service.call('continue_task', dict(request_id='child', task_id=self.task_id, prompt='continue'))
        self.release.set()
        self.assertEqual(self.service.wait_for_task(self.task_id, timeout_seconds=3)['state'], 'succeeded')
    def test_cancel_waiting_and_reject_resume(self):
        self.service.call('cancel_task', {'task_id': self.task_id})
        self.assertEqual(self.service.wait_for_task(self.task_id, timeout_seconds=3)['state'], 'cancelled')
        with self.assertRaises(HubError):
            self.service.call('resume_task', dict(request_id='r', task_id=self.task_id, intervention_id='wait1'))
    def test_expired_wait_rejects_resume(self):
        task = self.service.store.get(self.task_id)
        self.service.store.update(self.task_id, intervention=dict(task['intervention'], wait_deadline=time.time()-1))
        with self.assertRaises(HubError) as error:
            self.service.call('resume_task', dict(request_id='r', task_id=self.task_id, intervention_id='wait1'))
        self.assertEqual(error.exception.code, 'USER_WAIT_EXPIRED')
    def test_restart_quarantines_waiting_task(self):
        self.service.store.recover()
        task = self.service.store.get(self.task_id)
        self.assertEqual(task['state'], 'interrupted')
        self.assertTrue(task['workspace_quarantined'])


class MuseFlowTests(unittest.TestCase):
    def test_block_then_manual_confirmation_then_original_task_result(self):
        harness = recovery.MuseRecoveryTests()
        original_execute = harness.execute
        with tempfile.TemporaryDirectory() as td:
            # 直接执行 worker 契约，确认文件由人工确认入口写入。
            from agent_hub.adapters.muse import Muse
            real = Muse.execute
            def wrapped(adapter, task, cancel, event):
                task['log_dir'] = td
                adapter.env['HUB_MUSE_STATE_DIR'] = td
                def capture(kind, payload):
                    event(kind, payload)
                    if kind == 'waiting_for_user':
                        Path(td, 'muse-resume.json').write_text(json.dumps(
                            dict(intervention_id=payload['intervention_id'])))
                return real(adapter, task, cancel, capture)
            wait = [message(2, '请接管 [HUB_WAIT:recovery:human_verification]')]
            with patch.object(Muse, 'execute', wrapped):
                result = original_execute({'muse_raw': [{'session_id': 'sid'}],
                    'muse_send': [{'sent': True}, {'sent': True}],
                    'muse_history': [[], wait, wait, harness.reply(), harness.reply()]})
        self.assertIn('done', result['answer'])
        sends = [args for name, args in harness.arguments if name == 'muse_send']
        self.assertEqual(len(sends), 2)  # 原派发 + 一次明确确认后的继续
        self.assertEqual({args['thread'] for args in sends}, {'sid'})
        self.assertEqual(sum(kind == 'waiting_for_user' for kind, _ in harness.events), 1)
        self.assertEqual(sum(kind == 'user_wait_resumed' for kind, _ in harness.events), 1)

    def test_completion_before_confirmation_does_not_send_extra_turn(self):
        harness = recovery.MuseRecoveryTests()
        from agent_hub.adapters.muse import Muse
        real = Muse.execute
        with tempfile.TemporaryDirectory() as td:
            def wrapped(adapter, task, cancel, event):
                task['log_dir'] = td
                adapter.env['HUB_MUSE_STATE_DIR'] = td
                return real(adapter, task, cancel, event)
            def finish_and_confirm():
                Path(td, 'muse-resume.json').write_text(json.dumps(
                    dict(intervention_id='recovery:2')))
                return harness.reply()
            with patch.object(Muse, 'execute', wrapped):
                result = harness.execute({'muse_raw': [{'session_id': 'sid'}],
                    'muse_send': [{'sent': True}], 'muse_history': [[],
                        [message(2, '[HUB_WAIT:recovery:login]')], finish_and_confirm, harness.reply()]})
        self.assertIn('done', result['answer'])
        self.assertEqual(harness.calls.count('muse_send'), 1)

    def test_earlier_final_reply_cannot_complete_a_later_wait(self):
        harness = recovery.MuseRecoveryTests()
        from agent_hub.adapters.muse import Muse
        real = Muse.execute
        rows = harness.reply() + [message(10, '[HUB_WAIT:recovery:login]')]
        newer = [message(11, 'new completion [HUB:recovery]')]
        with tempfile.TemporaryDirectory() as td:
            def wrapped(adapter, task, cancel, event):
                task['log_dir'] = td
                adapter.env['HUB_MUSE_STATE_DIR'] = td
                def capture(kind, payload):
                    event(kind, payload)
                    if kind == 'waiting_for_user':
                        Path(td, 'muse-resume.json').write_text(json.dumps(
                            dict(intervention_id=payload['intervention_id'])))
                return real(adapter, task, cancel, capture)
            with patch.object(Muse, 'execute', wrapped):
                result = harness.execute({'muse_raw': [{'session_id': 'sid'}],
                    'muse_send': [{'sent': True}, {'sent': True}],
                    'muse_history': [[], rows, rows, rows, newer, newer]})
        self.assertEqual(result['completion_evidence']['reply_seq'], 11)
        self.assertEqual(harness.calls.count('muse_send'), 2)

    def test_waiting_with_final_marker_does_not_claim_success_and_expires(self):
        harness = recovery.MuseRecoveryTests()
        wait = [message(2, '请处理 [HUB_WAIT:recovery:login] [HUB:recovery]')]
        with patch('agent_hub.adapters.muse_wait.USER_WAIT_SECONDS', .2):
            with self.assertRaises(HubError) as error:
                harness.execute({'muse_raw': [{'session_id': 'sid'}],
                    'muse_send': [{'sent': True}], 'muse_history': [[], wait]})
        self.assertEqual(error.exception.code, 'TIMED_OUT')
        self.assertEqual(harness.calls.count('muse_send'), 1)


class ProcessDeadlineTests(unittest.TestCase):
    def test_worker_event_extends_parent_process_deadline(self):
        self.run_worker(extend=True)

    def test_worker_without_extension_expires_after_ready(self):
        with self.assertRaises(HubError) as caught:
            self.run_worker(extend=False)
        self.assertEqual(caught.exception.code, 'TIMED_OUT')

    def run_worker(self, extend):
        import sys
        from agent_hub.processes import run_process, environment
        with tempfile.TemporaryDirectory() as td:
            # Startup time varies on Windows; arm the short deadline after ready.
            deadline = [time.time() + 10]
            def event(kind, data):
                if kind == 'worker_ready':
                    deadline[0] = time.time() + .4
                if kind == 'waiting_for_user' and extend:
                    deadline[0] = time.time() + 2
            code = "import json,time; print(json.dumps({'hub_event':{'kind':'worker_ready','data':{}}}),flush=True); print(json.dumps({'hub_event':{'kind':'waiting_for_user','data':{}}}),flush=True); time.sleep(.6); print('done')"
            rc, stdout, _, _ = run_process([sys.executable, '-c', code], td, environment(),
                threading.Event(), lambda: deadline[0], td, event)
            self.assertEqual(rc, 0)
            self.assertIn('done', stdout)



if __name__ == '__main__':
    unittest.main()
