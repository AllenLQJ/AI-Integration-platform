import json
import tempfile
import unittest
import asyncio
import threading
import time
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import patch
from pathlib import Path
from agent_hub.adapters.codex import Codex
from agent_hub.adapters.claude import Claude
from agent_hub.adapters.muse import Muse
from agent_hub.models import HubError


class AdapterTests(unittest.TestCase):
    def test_codex_parse_and_resume_policy(self):
        adapter = Codex('codex.exe')
        output = '\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'thread_id': 'sid'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
            {'type': 'turn.completed'}])
        self.assertEqual(adapter.parse(output)['answer'], 'done')
        args = adapter.command({'prompt': 'hi', 'mode': 'read_only', 'resume_session': 'sid'})
        self.assertIn('resume', args)
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertIn('--ignore-user-config', args)
    def test_claude_tools_and_parse(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = Claude('claude.exe')
            read = adapter.command(dict(prompt='hi', mode='read_only', log_dir=td))
            edit = adapter.command(dict(prompt='hi', mode='edit_worktree', log_dir=td))
            self.assertIn('--tools=', read)
            self.assertIn('--tools=Read,Glob,Grep,Edit,Write', edit)
            self.assertIn('--strict-mcp-config', edit)
            result = adapter.parse('{"type":"result","result":"done","session_id":"sid","is_error":false}')
            self.assertEqual(result['session_id'], 'sid')
    def test_muse_requires_marker_and_new_sequence(self):
        marker = '[HUB:123]'
        events = [{'seq': 1, 'role': 'assistant', 'text': marker+' old'},
                  {'seq': 3, 'role': 'assistant', 'text': 'accepted'},
                  {'seq': 4, 'role': 'assistant', 'text': marker+'\n真实结果'}]
        reply = Muse.find_reply(events, 1, marker)
        self.assertEqual(reply['seq'], 4)
        self.assertIsNone(Muse.find_reply(events[:2], 1, marker))
    def test_muse_link_presentation_is_part_of_answer(self):
        reply = {'seq': 10, 'role':'assistant','text':'[HUB:x] answer'}
        rows = [reply, {'seq':11,'event_name':'delta.presentation','role':'assistant',
                'text':'official link', 'raw':{'payload':{'kind':'external_link',
                'data':{'url':'https://www.sqlite.org/lang_transaction.html'}}}}]
        answer, links = Muse.answer_with_links(rows, reply)
        self.assertIn('https://www.sqlite.org/lang_transaction.html', answer)
        self.assertEqual(len(links), 1)
    def test_muse_unwrap_preserves_uncertain_delivery(self):
        from exceptiongroup import ExceptionGroup
        wrapped = ExceptionGroup('outer', [ExceptionGroup('inner', [HubError('DELIVERY_UNKNOWN', 'unknown')])])
        self.assertEqual(Muse.unwrap(wrapped).code, 'DELIVERY_UNKNOWN')

    def test_muse_inline_links_are_returned_without_duplicates(self):
        reply = {'seq': 10, 'text': '官方 [事务](https://www.sqlite.org/lang_transaction.html)。'}
        rows = [reply, {'seq': 11, 'event_name': 'delta.presentation',
                'raw': {'payload': {'kind': 'external_link',
                'data': {'url': 'https://www.sqlite.org/lang_transaction.html'}}}}]
        answer, links = Muse.answer_with_links([reply], reply)
        self.assertEqual(links, ['https://www.sqlite.org/lang_transaction.html'])
        answer, links = Muse.answer_with_links(rows, reply)
        self.assertEqual(len(links), 1)
        self.assertEqual(answer.count(links[0]), 1)
    def test_muse_disconnect_after_send_is_remote_unknown(self):
        events = []
        class Session:
            sent = False
            async def initialize(self): pass
            async def call_tool(self, name, args, **kw):
                if name == 'muse_raw': payload = {'result': {'session_id': 'sid'}}
                elif name == 'muse_send': self.sent=True; payload = {'ok': True, 'sent': True}
                elif self.sent: raise OSError('synthetic disconnected')
                else: payload = []
                return SimpleNamespace(isError=False, structuredContent={'result': payload})
        @asynccontextmanager
        async def transport(*args): yield (None, None)
        @asynccontextmanager
        async def session(*args): yield Session()
        with patch('mcp.client.stdio.stdio_client', transport), patch('mcp.ClientSession', session):
            with self.assertRaises(HubError) as error:
                asyncio.run(Muse('unused').execute(dict(id='123', prompt='hello', deadline=time.time()+10),
                                   threading.Event(), lambda *args: events.append(args)))
        self.assertEqual(error.exception.code, 'REMOTE_STATE_UNKNOWN')
        self.assertIn('session', [e[0] for e in events])
        self.assertIn('accepted', [e[0] for e in events])

if __name__ == '__main__': unittest.main()
