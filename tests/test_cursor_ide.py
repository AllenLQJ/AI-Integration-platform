import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from agent_hub.adapters.cursor_ide import CursorIDE
from agent_hub.config import adapters
from agent_hub.models import HubError


class CursorIDEBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = CursorIDE(self.temp.name)
        self.cancel = threading.Event()
        self.result = {}
        self.task = dict(id='probe', prompt='真实任务', workspace=self.temp.name)
        def run():
            try:
                self.result['value'] = self.adapter.run(self.task, self.cancel, lambda *args: None)
            except HubError as error:
                self.result['error'] = error
        self.thread = threading.Thread(target=run)
        self.thread.start()
        def stop():
            self.cancel.set()
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
        self.addCleanup(stop)
        end = time.monotonic() + 2
        while 'probe' not in self.adapter.active and time.monotonic() < end:
            time.sleep(.01)

    def claim(self):
        return self.adapter.next_task('worker')['task']

    def test_claim_once_and_matching_result(self):
        self.assertEqual(self.claim()['prompt'], '真实任务')
        self.assertFalse(self.adapter.next_task('other')['ready'])
        self.adapter.update('probe', 'worker', 'dispatched', 'ide-session')
        with self.assertRaises(HubError):
            self.adapter.update('probe', 'worker', 'succeeded', 'wrong', answer='回答')
        self.adapter.update('probe', 'worker', 'succeeded', 'ide-session', answer='回答')
        self.thread.join(2)
        self.assertEqual(self.result['value']['answer'], '回答')

    def test_cannot_complete_unsubmitted_or_other_worker_task(self):
        self.claim()
        with self.assertRaises(HubError):
            self.adapter.update('probe', 'worker', 'succeeded', 'ide-session', answer='回答')
        with self.assertRaises(HubError):
            self.adapter.update('probe', 'other', 'failed', error='额度耗尽')
        self.adapter.update('probe', 'worker', 'failed', error='额度耗尽')
        self.thread.join(2)
        self.assertEqual(self.result['error'].code, 'CURSOR_IDE_FAILED')

    def test_cancel_dispatched_retains_uncertainty_and_blocks_new_dispatch(self):
        self.claim()
        self.adapter.update('probe', 'worker', 'dispatched', 'ide-session')
        self.cancel.set()
        self.thread.join(2)
        self.assertTrue(self.result['error'].details['execution_uncertain'])
        with self.assertRaises(HubError):
            self.adapter.update('probe', 'worker', 'succeeded', 'ide-session', answer='迟到')
        restarted = CursorIDE(self.temp.name)
        with self.assertRaises(HubError):
            restarted.next_task('worker')

    def test_default_ide_and_explicit_cli_config(self):
        Path(self.temp.name, 'config.json').write_text(json.dumps({'agents': {
            'cursor': {'kind': 'ide'}, 'cursor_cli': {'executable': '/some/agent'}}}))
        configured = adapters(self.temp.name)
        self.assertIsInstance(configured['cursor'], CursorIDE)
        self.assertEqual(configured['cursor_cli'].executable, '/some/agent')
