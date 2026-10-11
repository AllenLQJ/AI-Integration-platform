"""验证回复关联、单次发送、崩溃隔离和 Cursor 配置边界，不消耗模型额度。"""
import io
import itertools
import hashlib
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from agent_hub.adapters.cursor import Cursor
from agent_hub.adapters.grokbot import GrokBot
from agent_hub.models import HubError
from agent_hub.config import adapters
from agent_hub.__main__ import main


class CursorTests(unittest.TestCase):
    def test_only_terminal_success_is_result(self):
        c = Cursor('unused')
        result = dict(type='result', subtype='success', is_error=False, result='done', session_id='sid')
        self.assertEqual(c.parse(json.dumps(result))['answer'], 'done')
        for payload in [dict(result, is_error=True), dict(result, subtype='error'),
                        dict(result, session_id=None), {'type': 'assistant', 'result': 'done'}, []]:
            with self.subTest(payload=payload), self.assertRaises(HubError):
                c.parse(json.dumps(payload))

    def test_config_isolation_modes_and_resume(self):
        with tempfile.TemporaryDirectory() as td:
            task = dict(workspace=td, log_dir=str(Path(td)/'data/logs/task-a'), mode='read_only',
                        prompt='read', resume_session='native')
            c = Cursor('unused')
            with patch('agent_hub.adapters.cursor.CLI.run', return_value={}) as run:
                c.run(task, threading.Event(), lambda *args: None)
            isolated = run.call_args.args[0]
            root = Path(isolated.env['CURSOR_CONFIG_DIR'])
            self.assertEqual(json.loads((root/'mcp.json').read_text()), {'mcpServers': {}})
            permissions = json.loads((root/'cli-config.json').read_text())['permissions']
            self.assertIn('Write(*)', permissions['deny'])
            self.assertIn('Shell(*)', permissions['deny'])
            self.assertIn('Mcp(*:*)', permissions['deny'])
            next_task = dict(task, log_dir=str(Path(td)/'data/logs/task-b'))
            with patch('agent_hub.adapters.cursor.CLI.run', return_value={}) as next_run:
                c.run(next_task, threading.Event(), lambda *args: None)
            # 不因任务日志目录变化而丢失原生聊天。
            self.assertEqual(next_run.call_args.args[0].env['CURSOR_CONFIG_DIR'], str(root))
            args = c.command(task)
            self.assertIn('ask', args)
            self.assertIn('native', args)
            for flag in ('--force', '--approve-mcps', '--yolo'):
                self.assertNotIn(flag, args)
            (Path(td)/'.cursor').mkdir()
            (Path(td)/'.cursor/mcp.json').write_text('{}')
            with self.assertRaises(HubError) as error:
                c.run(task, threading.Event(), lambda *args: None)
            self.assertEqual(error.exception.code, 'UNSAFE_PROJECT_CONFIG')


class GrokBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bot = GrokBot('Hub Worker', state_file=self.root/'remote.json', env={'CURSOR_API_KEY': 'fixture'})
        self.task = dict(id=uuid.uuid4().hex, prompt='hello', deadline=time.time()+10)
        self.sid = str(uuid.uuid4())
        self.calls = []
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, cursor='10', entries=None, busy=False):
        return dict(entries=entries or [], latestUpdatedSeq=cursor,
                    turn=dict(idle=not busy, inFlight=busy, queued=0))

    def responses(self, pages, send=None):
        pages = iter(pages)
        def request(method, path, body, timeout):
            self.calls.append((method, path, body))
            if path == '/v0/grokbot/sessions':
                return dict(id=self.sid, name='Hub Worker', latestUpdatedSeq='10')
            if method == 'POST':
                if isinstance(send, Exception):
                    raise send
                return send or {'delivery': 'accepted_temporal'}
            return next(pages, self.page())
        self.bot.request = request

    def run_bot(self, cancel=None):
        return self.bot.run(self.task, cancel or threading.Event(), lambda *e: self.events.append(e))

    def test_only_new_delivered_assistant_completion_finishes(self):
        marker = '[HUB-END:' + self.task['id'] + ']'
        entries = [dict(seq='9', updatedSeq='9', kind='send-message', role='assistant', text=marker),
                   dict(seq='11', updatedSeq='12', kind='thought', role='assistant', text=marker),
                   dict(seq='12', updatedSeq='13', kind='send-message', role='user', text=marker),
                   dict(seq='13', updatedSeq='14', kind='send-message', role='assistant', text='actual\n'+marker)]
        self.responses([self.page(), self.page('14', entries, True)] + [self.page('14')]*4)
        result = self.run_bot()
        self.assertEqual(result['answer'], 'actual\n'+marker)
        self.assertFalse(self.bot.state_file.exists())
        self.assertEqual(len([c for c in self.calls if c[1].endswith('/messages')]), 1)
        self.assertIn(('accepted', {'baseline': 10}), self.events)

    def test_busy_bot_does_not_send(self):
        self.responses([self.page(busy=True)])
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'WORKSPACE_BUSY')
        self.assertFalse(any(c[1].endswith('/messages') for c in self.calls))

    def test_pinned_existing_session_never_gets_or_creates_a_bot(self):
        self.bot.session_id = self.sid
        self.responses([self.page(busy=True)])
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code,'WORKSPACE_BUSY')
        self.assertFalse(any(c[1]=='/v0/grokbot/sessions' for c in self.calls))

    def test_pinned_session_cannot_resume_another_session(self):
        self.bot.session_id = self.sid
        self.task['resume_session'] = 'another-session'
        self.responses([])
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code,'SESSION_UNAVAILABLE')
        self.assertFalse(self.calls)

    def test_session_aliases_share_lock_and_block_each_other(self):
        env = dict(CURSOR_API_KEY='fixture',AGENT_SERVE_CONFIG_DIR=str(self.root))
        first = GrokBot('first alias',env=env,session_id=self.sid)
        second = GrokBot('second alias',env=env,session_id=self.sid)
        self.assertEqual(first.state_file,second.state_file)
        first.reserve(self.task,self.sid)
        with self.assertRaises(HubError) as error:
            second.run(self.task,threading.Event(),lambda *args:None)
        self.assertEqual(error.exception.code,'REMOTE_STATE_UNKNOWN')

    def test_old_name_lock_is_not_silently_ignored_when_pinning_session(self):
        env = dict(CURSOR_API_KEY='fixture',AGENT_SERVE_CONFIG_DIR=str(self.root))
        old = self.root/('hub-grokbot-'+hashlib.sha256(b'old alias').hexdigest()[:16]+'.json')
        old.write_text(json.dumps(dict(task_id='old',session_id=self.sid,bot_name='old alias')))
        bot = GrokBot('new alias',env=env,session_id=self.sid)
        with self.assertRaises(HubError) as error:
            bot.run(self.task,threading.Event(),lambda *args:None)
        self.assertEqual(error.exception.code,'REMOTE_STATE_UNKNOWN')
        self.assertTrue(old.exists())

    def test_send_uncertainty_is_never_retried_and_blocks_next_task(self):
        self.responses([self.page()], OSError('disconnected'))
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'DELIVERY_UNKNOWN')
        self.assertEqual(len([c for c in self.calls if c[1].endswith('/messages')]), 1)
        self.assertTrue(self.bot.state_file.exists())
        previous = len(self.calls)
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'REMOTE_STATE_UNKNOWN')
        self.assertEqual(len(self.calls), previous)

    def test_idle_and_old_replies_do_not_mean_success(self):
        marker = '[HUB-END:other]'
        self.responses([self.page(), self.page('11', [dict(seq='11',updatedSeq='11',
            kind='send-message',role='assistant',text=marker)])] + [self.page('11')]*4)
        count = 0
        def checkpoint(task, cancel):
            nonlocal count
            count += 1
            if count > 12:
                raise HubError('TIMED_OUT', 'fixture deadline')
        self.bot.checkpoint = checkpoint
        self.bot.cancellation_timeout = .001
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'TIMED_OUT')
        self.assertTrue(self.bot.state_file.exists())

    def test_cancel_after_send_confirms_interrupt_and_idle(self):
        cancel = threading.Event()
        self.responses([self.page()])
        original = self.bot.request
        cancelled_pages = None
        def request(*args):
            nonlocal cancelled_pages
            if cancelled_pages is not None and args[0] == 'GET':
                self.calls.append(args[:3])
                return next(cancelled_pages)
            result = original(*args)
            if args[1].endswith('/messages'):
                entry = dict(seq='11',updatedSeq='11',kind='message',role='user',text=args[2]['text'])
                cancelled_pages = iter([self.page('11',[entry],True),self.page('11',busy=True)]+[self.page('11')]*4)
                cancel.set()
            return result
        self.bot.request = request
        with patch('agent_hub.adapters.grokbot.time.sleep'), self.assertRaises(HubError) as error:
            self.run_bot(cancel)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertFalse(self.bot.state_file.exists())
        self.assertTrue(error.exception.details['remote_cancelled'])
        self.assertEqual(len([c for c in self.calls if c[1].endswith('/interrupt')]),1)
        self.assertEqual(error.exception.details['cancellation_evidence']['idle_polls'],4)

    def cancellation_fixture(self, pages):
        prompt = 'exact submitted prompt'
        self.bot.reserve(self.task,self.sid)
        context = dict(sid=self.sid,baseline=10,prompt=prompt,accepted=True)
        own = dict(seq='11',updatedSeq='11',kind='message',role='user',text=prompt)
        self.responses([self.page('11',[own],True)]+pages)
        return context

    def test_interrupt_acknowledgement_without_idle_is_not_confirmation(self):
        context = self.cancellation_fixture([self.page('11',busy=True)]*20)
        self.bot.cancellation_timeout = 1
        ticks = itertools.count()
        with patch('agent_hub.adapters.grokbot.time.sleep'), \
             patch('agent_hub.adapters.grokbot.time.monotonic', side_effect=lambda: next(ticks)*.1):
            result = self.bot.cancel_remote(self.task,context)
        self.assertFalse(result['remote_cancelled'])
        self.assertTrue(result['interrupt_acknowledged'])
        self.assertTrue(self.bot.state_file.exists())
        self.assertEqual(len([c for c in self.calls if c[1].endswith('/interrupt')]),1)

    def test_interrupt_is_never_retried_on_uncertain_delivery(self):
        context = self.cancellation_fixture([self.page('11',busy=True)])
        original = self.bot.request
        def request(*args):
            if args[1].endswith('/interrupt'):
                self.calls.append(args[:3])
                raise OSError('connection lost')
            return original(*args)
        self.bot.request = request
        with patch('agent_hub.adapters.grokbot.time.sleep'):
            result = self.bot.cancel_remote(self.task,context)
        self.assertFalse(result['remote_cancelled'])
        self.assertTrue(result['interrupt_requested'])
        self.assertFalse(result['interrupt_acknowledged'])
        self.assertEqual(len([c for c in self.calls if c[1].endswith('/interrupt')]),1)
        self.assertTrue(self.bot.state_file.exists())

    def test_foreign_message_prevents_session_interrupt(self):
        foreign = dict(seq='12',updatedSeq='12',kind='message',role='user',text='human next turn')
        context = self.cancellation_fixture([self.page('12',[foreign],True)])
        with patch('agent_hub.adapters.grokbot.time.sleep'):
            result = self.bot.cancel_remote(self.task,context)
        self.assertFalse(result['remote_cancelled'])
        self.assertFalse(any(c[1].endswith('/interrupt') for c in self.calls))
        self.assertTrue(self.bot.state_file.exists())

    def test_changed_lock_prevents_interrupt_or_clearing_another_task(self):
        context = self.cancellation_fixture([])
        self.bot.state_file.write_text(json.dumps(dict(task_id='other',session_id=self.sid,bot_name=self.bot.name)))
        result = self.bot.cancel_remote(self.task,context)
        self.assertFalse(result['remote_cancelled'])
        self.assertFalse(self.calls)
        self.assertTrue(self.bot.state_file.exists())

    def test_idle_without_interrupt_can_confirm_already_finished(self):
        completion = dict(seq='12',updatedSeq='12',kind='send-message',text='done\n[HUB-END:'+self.task['id']+']')
        context = self.cancellation_fixture([self.page('12',[completion])]+[self.page('12')]*4)
        with patch('agent_hub.adapters.grokbot.time.sleep'):
            result = self.bot.cancel_remote(self.task,context)
        self.assertTrue(result['remote_cancelled'])
        self.assertFalse(result['interrupt_requested'])
        self.assertEqual(result['cancellation_evidence']['stop_reason'],'already_idle')
        self.assertFalse(self.bot.state_file.exists())

    def test_pre_start_idle_cannot_clear_lock_before_late_busy_turn(self):
        prompt = 'exact submitted prompt'
        context = self.cancellation_fixture([])
        own = dict(seq='11',updatedSeq='11',kind='message',role='user',text=prompt)
        self.responses([self.page('11',[own])]+[self.page('11')]*4+
                       [self.page('11',busy=True)]+[self.page('11')]*4)
        with patch('agent_hub.adapters.grokbot.time.sleep'):
            result = self.bot.cancel_remote(self.task,context)
        self.assertTrue(result['remote_cancelled'])
        self.assertTrue(result['interrupt_requested'])
        self.assertEqual(result['cancellation_evidence']['stop_reason'],'interrupt')

    def test_unknown_acceptance_cannot_interrupt(self):
        self.bot.reserve(self.task,self.sid)
        result = self.bot.cancel_remote(self.task,{})
        self.assertFalse(result['remote_cancelled'])
        self.assertFalse(self.calls)
        self.assertTrue(self.bot.state_file.exists())

    def test_resume_cannot_switch_named_session(self):
        self.task['resume_session'] = str(uuid.uuid4())
        self.responses([])
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'SESSION_UNAVAILABLE')
        self.assertFalse(any(c[1].endswith('/messages') for c in self.calls))

    def test_read_failure_after_acceptance_keeps_remote_unknown(self):
        self.responses([self.page()])
        request = self.bot.request
        def disconnected(*args):
            if self.bot.state_file.exists() and args[0] == 'GET':
                raise OSError('read failure')
            return request(*args)
        self.bot.request = disconnected
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'REMOTE_STATE_UNKNOWN')
        self.assertTrue(self.bot.state_file.exists())

    def test_malformed_history_fails_closed(self):
        for page in [dict(entries=[], latestUpdatedSeq='oops', turn={}),
                     self.page(entries=[{'seq':'bad','updatedSeq':'1'}]),
                     dict(self.page(), turn={'idle':True, 'inFlight':False, 'queued':False})]:
            with self.subTest(page=page), self.assertRaises(HubError):
                GrokBot.page(page)

    def test_real_api_empty_cursor_and_numeric_session(self):
        self.sid = '12247914'
        self.assertEqual(GrokBot.page(self.page('-1'))[1], '-1')
        self.responses([self.page(busy=True)])
        with self.assertRaises(HubError) as error:
            self.run_bot()
        self.assertEqual(error.exception.code, 'WORKSPACE_BUSY')


class EntryTests(unittest.TestCase):
    def test_opt_in_config_and_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(adapters(root), {})
            (root/'config.json').write_text(json.dumps({'agents':{
                'cursor':{'executable':'unused'},'grokbot':{'bot_name':'Hub Worker','session_id':'12345'}}}))
            loaded = adapters(root)
            self.assertEqual(set(loaded), {'cursor','grokbot'})
            self.assertEqual(loaded['grokbot'].session_id,'12345')
            self.assertEqual(loaded['grokbot'].state_file,
                             GrokBot('Hub Worker',session_id='12345').state_file)

    def test_rpc_reads_json_from_stdin_and_calls_only_named_tool(self):
        with patch('sys.argv', ['agent-hub','rpc','get_result','--arguments','-']), \
             patch('sys.stdin', io.StringIO('{"task_id":"one"}')), \
             patch('sys.stdout', new_callable=io.StringIO) as output, \
             patch('agent_hub.client.Client') as client:
            client.return_value.call.return_value = {'ready':False}
            main()
            client.return_value.call.assert_called_once_with('get_result',{'task_id':'one'})
            self.assertEqual(json.loads(output.getvalue()),{'ready':False})

    def test_rpc_rejects_non_object_or_unknown_tool_before_client(self):
        for argv in [['rpc','get_result','--arguments','[]'], ['rpc','shutdown'], ['rpc']]:
            with self.subTest(argv=argv), patch('sys.argv',['agent-hub']+argv), \
                 patch('sys.stderr', new_callable=io.StringIO), patch('agent_hub.client.Client') as client:
                with self.assertRaises(SystemExit):
                    main()
                client.assert_not_called()
