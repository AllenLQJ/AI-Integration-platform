import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_hub.models import HubError
from agent_hub.processes import environment, log_record, run_process, stream_events


class StreamEventExtractionTests(unittest.TestCase):
    def kinds(self, data):
        return list(stream_events(data))

    def test_codex_command_file_and_test_shapes(self):
        started = {
            'type': 'item.started',
            'item': {'id': 'cmd-1', 'type': 'command_execution', 'command': 'pytest -q', 'status': 'in_progress'},
        }
        completed = {
            'type': 'item.completed',
            'item': {
                'id': 'cmd-1', 'type': 'command_execution', 'command': 'pytest -q',
                'exit_code': 0, 'aggregated_output': 'ok\n', 'is_test': True,
                'test_summary': 'passed=3 failed=0',
            },
        }
        read_item = {
            'type': 'item.completed',
            'item': {'id': 'read-1', 'type': 'file_read', 'path': 'src/a.py', 'offset': 0, 'limit': 40},
        }
        change = {
            'type': 'item.completed',
            'item': {'id': 'chg-1', 'type': 'file_change', 'path': 'src/a.py', 'changes': '+print(1)'},
        }
        unknown = {'type': 'item.completed', 'item': {'type': 'mystery_widget', 'payload': {'x': 1}}}
        events = self.kinds(started) + self.kinds(completed) + self.kinds(read_item) + self.kinds(change) + self.kinds(unknown)
        activities = [payload for kind, payload in events if kind == 'activity']
        tools = [payload for kind, payload in events if kind == 'tool']
        self.assertTrue(any(a.get('operation_id') == 'cmd-1' and a['status'] == 'started' for a in activities))
        self.assertTrue(any(a.get('operation_id') == 'cmd-1' and a['kind'] == 'test' for a in activities))
        self.assertTrue(any(a.get('kind') == 'file_read' and a.get('operation', {}).get('path') == 'src/a.py' for a in activities))
        self.assertTrue(any(a.get('kind') == 'file_change' for a in activities))
        self.assertTrue(any(t.get('name') == 'mystery_widget' and t.get('completeness') == 'unavailable' for t in tools))
        # Substring "test" in a non-test command must not become test evidence.
        plain = {
            'type': 'item.completed',
            'item': {'id': 'c2', 'type': 'command_execution', 'command': 'echo testing', 'exit_code': 0},
        }
        plain_acts = [p for k, p in self.kinds(plain) if k == 'activity']
        self.assertTrue(all(a['kind'] == 'command' for a in plain_acts))

    def test_claude_tool_use_extracts_path_before_log_redaction(self):
        data = {
            'type': 'assistant',
            'message': {
                'content': [
                    {'type': 'tool_use', 'id': 'tu1', 'name': 'Read',
                     'input': {'file_path': '/tmp/ws/src/a.py', 'secret': 'should-not-matter'}},
                    {'type': 'tool_result', 'tool_use_id': 'tu1', 'is_error': False,
                     'content': 'FILE BODY MUST NOT BE REQUIRED'},
                ]
            }
        }
        events = self.kinds(data)
        activity = next(p for k, p in events if k == 'activity' and p.get('phase') == 'started')
        self.assertEqual(activity['operation_id'], 'tu1')
        self.assertEqual(activity['kind'], 'file_read')
        self.assertIn('a.py', activity['operation']['path_display'])
        logged = log_record(data)
        tool_use = logged['message']['content'][0]
        self.assertNotIn('input', tool_use)
        self.assertEqual(tool_use.get('id'), 'tu1')

    def test_unicode_missing_ids_and_cursor_tool_call(self):
        data = {
            'type': 'tool_call',
            'name': 'Shell',
            'tool_call': {'shellToolCall': {'command': 'echo 中文'}},
        }
        events = self.kinds(data)
        activity = next(p for k, p in events if k == 'activity')
        self.assertEqual(activity['kind'], 'command')
        self.assertIn('中文', activity['operation']['command_display'])
        self.assertIsNone(activity.get('operation_id'))
        missing = {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'ls'}}
        acts = [p for k, p in self.kinds(missing) if k == 'activity']
        self.assertTrue(acts and acts[0].get('operation_id') is None)

    def test_user_prompt_echo_keeps_user_role(self):
        user = {
            'type': 'user',
            'uuid': 'u-1',
            'message': {'id': 'm-user', 'content': [{'type': 'text', 'text': 'please implement'}]},
        }
        assistant = {
            'type': 'assistant',
            'message': {'id': 'm-as', 'revision': 2, 'content': [{'type': 'text', 'text': 'working'}]},
        }
        events = self.kinds(user) + self.kinds(assistant)
        messages = [p for k, p in events if k == 'message']
        self.assertEqual(messages[0]['role'], 'user')
        self.assertEqual(messages[0]['provider_message_id'], 'm-user')
        self.assertEqual(messages[1]['role'], 'assistant')
        self.assertEqual(messages[1]['provider_revision'], 2)

    def test_command_v1_fake_hub_event_and_stdout_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            worker = Path(td) / 'worker.py'
            worker.write_text(
                'import json\n'
                'print(json.dumps({"type":"system","subtype":"init","session_id":"fake"}))\n'
                'print(json.dumps({"hub_event":{"kind":"accepted","data":{}}}))\n'
                'print(json.dumps({"type":"item.completed","item":{"type":"command_execution","command":"x","id":"c"}}))\n'
                'print(json.dumps({"answer":"ok"}))\n',
                encoding='utf-8')
            events = []
            from agent_hub.adapters.command import Command
            from agent_hub.plugins import PluginContext
            context = PluginContext('custom', Path(td) / 'plugin')
            task = dict(id='t1', prompt='p', mode='delegate', workspace=td, cwd=td,
                        deadline=time.time() + 20, log_dir=str(Path(td) / 'logs'))
            Command(dict(args=[str(worker), '{request_file}']), context).run(
                task, threading.Event(), lambda k, d: events.append((k, d)))
            self.assertEqual([k for k, _ in events], ['process'])


if __name__ == '__main__':
    unittest.main()
