import asyncio
import json
import tempfile
import time
import unittest
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agent_hub.adapters.muse import Muse


class MuseCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.bot=Muse('fixture',env={'HUB_MUSE_STATE_DIR':self.temp.name})
        self.task=dict(id='cancel-test',prompt='calculate')
        self.context=dict(session_id='original-session',baseline=4)
        self.bot.reserve(self.task,self.context['session_id'])
        self.prompt='calculate\n完成本轮回答后，请在最终回答的最后一行逐字写出 [HUB:cancel-test]。不要在接收确认、进度消息或工具执行前写此标记。'
        self.history=[dict(seq=5,role='user',text=self.prompt)]
        self.calls=[]

    def tearDown(self):self.temp.cleanup()

    def execute(self, foreign=False, acknowledged=True, busy=False):
        outer=self
        class Session:
            async def initialize(self):pass
            async def call_tool(self,name,args,**kwargs):
                outer.calls.append((name,args))
                if name=='muse_history':
                    value=outer.history+([dict(seq=6,role='user',text='another')] if foreign else [])
                elif args['method']=='chat.cancel':
                    outer.assertEqual(args['body'],{'session_id':'original-session'})
                    value={'ok':True,'result':{'cancelled':acknowledged}}
                else:
                    value={'result':{'session_id':'original-session','status':'running' if busy else 'interrupted'}}
                return SimpleNamespace(isError=False,structuredContent={'result':value})
        @asynccontextmanager
        async def transport(*args):yield None,None
        @asynccontextmanager
        async def session(*args):yield Session()
        with patch('mcp.client.stdio.stdio_client',transport),patch('mcp.ClientSession',session):
            return asyncio.run(self.bot.cancel_remote(self.task,self.context))

    def test_original_session_ack_and_independent_stopped_status(self):
        result=self.execute()
        self.assertTrue(result['remote_cancelled'])
        self.assertEqual(result['cancellation_evidence']['status'],'interrupted')
        self.assertFalse(self.bot.state_file('original-session').exists())
        self.assertEqual(sum(args.get('method')=='chat.cancel' for _,args in self.calls),1)

    def test_foreign_message_preserves_lock_without_interrupt(self):
        result=self.execute(foreign=True)
        self.assertFalse(result['remote_cancelled'])
        self.assertTrue(self.bot.state_file('original-session').exists())
        self.assertFalse(any(args.get('method')=='chat.cancel' for _,args in self.calls))

    def test_changed_owner_is_not_cancelled(self):
        self.bot.state_file('original-session').write_text(json.dumps({'task_id':'other','session_id':'original-session'}))
        result=self.execute()
        self.assertFalse(result['remote_cancelled']);self.assertFalse(self.calls)

    def test_missing_acceptance_never_interrupts(self):
        self.context={}
        result=self.execute()
        self.assertFalse(result['remote_cancelled']);self.assertFalse(self.calls)

    def test_acknowledgement_while_still_running_preserves_lock(self):
        self.bot.cancellation_timeout=.1
        result=self.execute(busy=True)
        self.assertTrue(result['interrupt_acknowledged'])
        self.assertFalse(result['remote_cancelled'])
        self.assertTrue(self.bot.state_file('original-session').exists())

    def test_negative_acknowledgement_is_not_stopped(self):
        self.bot.cancellation_timeout=.1
        result=self.execute(acknowledged=False)
        self.assertFalse(result['remote_cancelled'])
        self.assertEqual(sum(args.get('method')=='chat.cancel' for _,args in self.calls),1)

    def test_worker_deadline_result_still_runs_remote_cleanup(self):
        root=Path(self.temp.name)
        task=dict(self.task,log_dir=str(root),workspace=str(root),deadline=time.time()-1)
        (root/'stdout.log').write_text(json.dumps({'hub_event':{'kind':'accepted','data':self.context}})+'\n')
        expired=json.dumps({'hub_result':{'error_code':'TIMED_OUT','error':'expired'}})
        confirmed=json.dumps({'hub_result':{'remote_cancelled':True}})
        from agent_hub.models import HubError
        with patch('agent_hub.adapters.muse.run_process',side_effect=[(0,expired,'',{}),(0,confirmed,'',{})]) as run:
            with self.assertRaises(HubError) as error:
                self.bot.run(task,threading.Event(),lambda *args:None)
        self.assertEqual(error.exception.code,'TIMED_OUT')
        self.assertTrue(error.exception.details['remote_cancelled'])
        self.assertIn('agent_hub.adapters.muse_cancel_worker',run.call_args.args[0])


    def test_resume_messages_owned_by_task_allow_cancellation(self):
        self.task['log_dir'] = self.temp.name
        expected = [self.prompt + '\n新增等待协议', '用户确认继续原任务']
        Path(self.temp.name, 'muse-owned-messages.json').write_text(json.dumps(expected))
        self.history = [dict(seq=5+i, role='user', text=text) for i, text in enumerate(expected)]
        self.assertTrue(self.execute()['remote_cancelled'])

    def test_foreign_message_after_resume_still_prevents_cancellation(self):
        self.task['log_dir'] = self.temp.name
        expected = [self.prompt, '用户确认继续原任务']
        Path(self.temp.name, 'muse-owned-messages.json').write_text(json.dumps(expected))
        self.history = [dict(seq=5+i, role='user', text=text) for i, text in enumerate(expected)]
        self.assertFalse(self.execute(foreign=True)['remote_cancelled'])
