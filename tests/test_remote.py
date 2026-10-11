import json
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub.adapters.remote import Remote
from agent_hub.daemon import make_server
from agent_hub.models import HubError
from agent_hub.service import Service
from agent_hub.ssh_client import SshClient


class SshTests(unittest.TestCase):
    def test_quotes_paths_and_sends_prompt_only_on_stdin(self):
        client = SshClient('known-host', '/space dir/python', '/remote data')
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([],0,'{"ok":true}','')) as run:
            self.assertEqual(client.call('submit_task', {'prompt':'$(unsafe)`cmd`'}), {'ok':True})
        argv = run.call_args.args[0]
        self.assertEqual(shlex.split(argv[-1])[0], '/space dir/python')
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertNotIn('unsafe', str(argv))
        self.assertIn('unsafe', run.call_args.kwargs['input'])

    def test_rejects_options_and_invalid_remote_paths(self):
        for host, python in [('-oProxyCommand=bad','/python'),('host;cmd','/python'),('host','python')]:
            with self.assertRaises(HubError): SshClient(host,python,'/data')

    def test_uncertain_send_is_not_repeated(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('ssh',1)) as run:
            with self.assertRaises(HubError) as error:
                SshClient('host','/python','/data').call('submit_task',{})
        self.assertEqual(error.exception.code,'DELIVERY_UNKNOWN')
        self.assertEqual(run.call_count,1)

    def test_malformed_remote_error_is_not_used_as_exception_code(self):
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],1,'{"error":{"code":[],"message":{}}}','')):
            with self.assertRaises(HubError) as error:
                SshClient('host','/python','/data').call('get_task',{})
        self.assertEqual(error.exception.code,'INVALID_RESPONSE')


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.adapter = Remote('fixture','/python','/data','worker','/workspace','read_only')
        self.task = dict(id='task',prompt='reply',deadline=time.time()+15,log_dir=str(self.root))
        self.events = []

    def tearDown(self): self.temp.cleanup()

    def test_continue_and_result_keep_target_task_binding(self):
        self.task['resume_session']='old-remote-task'
        with patch.object(self.adapter.client,'call',side_effect=[{'id':'new-remote-task'},
            {'ready':True,'state':'succeeded','answer':'actual','artifacts':{'workspace':'/remote/tree'}}]) as call:
            result = self.adapter.run(self.task,threading.Event(),lambda *e:self.events.append(e))
        self.assertEqual(call.call_args_list[0].args[0],'continue_task')
        self.assertEqual(call.call_args_list[0].args[1]['task_id'],'old-remote-task')
        self.assertEqual(result['session_id'],'new-remote-task')
        self.assertEqual(result['remote_artifacts']['workspace'],'/remote/tree')
        self.assertEqual(json.loads((self.root/'remote-task.json').read_text())['task_id'],'new-remote-task')

    def test_cancel_targets_original_task_and_confirms_cli_shutdown(self):
        cancel=threading.Event()
        def rpc(tool,args,**kwargs):
            if tool=='submit_task': cancel.set();return {'id':'original'}
            self.assertEqual(args['task_id'],'original')
            if tool=='cancel_task': return {'cancel_scope':'local_process_tree'}
            return {'ready':True,'state':'cancelled'}
        with patch.object(self.adapter.client,'call',side_effect=rpc):
            with self.assertRaises(HubError) as error:
                self.adapter.run(self.task,cancel,lambda *e:self.events.append(e))
        self.assertTrue(error.exception.details['remote_cancelled'])

    def test_cloud_local_wait_only_does_not_claim_remote_stop(self):
        with patch.object(self.adapter.client,'call',side_effect=[{'cancel_scope':'remote_session'},
            {'ready':True,'state':'cancelled','remote_cancelled':False}]):
            result = self.adapter.cancel_remote('original')
        self.assertFalse(result['remote_cancelled'])
        self.assertTrue(result['remote_task_terminal'])

    def test_read_disconnect_preserves_id_without_resubmitting(self):
        with patch.object(self.adapter.client,'call',side_effect=[{'id':'original'},HubError('REMOTE_UNAVAILABLE','gone')]) as call:
            with self.assertRaises(HubError) as error:
                self.adapter.run(self.task,threading.Event(),lambda *e:self.events.append(e))
        self.assertEqual(error.exception.code,'REMOTE_STATE_UNKNOWN')
        self.assertEqual([c.args[0] for c in call.call_args_list],['submit_task','get_result'])

    def test_malformed_result_keeps_original_remote_task_id(self):
        with patch.object(self.adapter.client,'call',side_effect=[{'id':'original'},[]]):
            with self.assertRaises(HubError) as error:
                self.adapter.run(self.task,threading.Event(),lambda *e:self.events.append(e))
        self.assertEqual(error.exception.code,'REMOTE_STATE_UNKNOWN')
        self.assertEqual(error.exception.details['remote_task']['task_id'],'original')

    def test_expired_wait_uses_independent_cancel_deadline(self):
        def rpc(tool,args,**kwargs):
            if tool=='submit_task':self.task['deadline']=time.time()-1;return {'id':'original'}
            self.assertEqual(args['task_id'],'original')
            self.assertGreater(kwargs['timeout'],0)
            if tool=='cancel_task':return {'cancel_scope':'local_process_tree'}
            return {'ready':True,'state':'timed_out'}
        with patch.object(self.adapter.client,'call',side_effect=rpc):
            with self.assertRaises(HubError) as error:
                self.adapter.run(self.task,threading.Event(),lambda *e:self.events.append(e))
        self.assertEqual(error.exception.code,'TIMED_OUT')
        self.assertTrue(error.exception.details['remote_cancelled'])

    @unittest.skipIf(sys.platform=='win32','POSIX SSH 命令模拟，仅在 Mac/Linux 运行')
    def test_bridge_cli_real_rpc_to_isolated_hub(self):
        class Worker:
            capabilities=['read_only','edit_worktree','continue'];remote_cancel=True
            def diagnose(self):return {'available':True}
            def run(self,task,cancel,event):
                if task['mode']=='edit_worktree':
                    (Path(task['workspace'])/'file.txt').write_text('REMOTE-EDIT')
                return {'answer':task['prompt'],'session_id':'native-session'}
        remote_root=self.root/'target';remote_root.mkdir()
        service=Service(remote_root,{'worker':Worker()})
        server=make_server(service,'fixture-token')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        (remote_root/'connection.json').write_text(json.dumps({'url':'http://127.0.0.1:'+str(server.server_port),'token':'fixture-token'}))
        shim=self.root/'ssh-fixture'
        shim.write_text('#!'+sys.executable+'\nimport sys,subprocess,shlex\np=subprocess.run(shlex.split(sys.argv[-1]),input=sys.stdin.read(),text=True)\nraise SystemExit(p.returncode)\n')
        shim.chmod(0o700)
        client=SshClient('fixture',sys.executable,str(remote_root),str(shim))
        try:
            agents=client.call('list_agents');self.assertTrue(agents['worker']['available'])
            args=dict(request_id='stable',agent='worker',cwd=str(self.root),mode='read_only',prompt='BRIDGE-UNIQUE',timeout_seconds=10)
            task=client.call('submit_task',args)
            self.assertEqual(client.call('submit_task',args)['id'],task['id'])
            result=client.call('get_result',{'task_id':task['id']})
            self.assertEqual(result['answer'],'BRIDGE-UNIQUE')
            self.assertEqual(client.call('continue_task',dict(task_id=task['id'],request_id='next',prompt='NEXT'))['parent_id'],task['id'])
            repo=self.root/'remote repo';repo.mkdir();(repo/'file.txt').write_text('BASE')
            for argv in [['git','init',str(repo)],['git','-C',str(repo),'add','file.txt'],
                ['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture']]:
                subprocess.run(argv,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            proxy=Remote('fixture',sys.executable,str(remote_root),'worker',str(repo),'edit_worktree')
            proxy.client=client
            controller=Service(self.root/'controller',{'target-worker':proxy})
            try:
                job=controller.call('submit_task',dict(request_id='proxy',agent='target-worker',cwd=str(self.root),mode='delegate',prompt='PROXY-MARKER',timeout_seconds=30))
                for _ in range(100):
                    result=controller.call('get_result',{'task_id':job['id']})
                    if result['ready']:break
                    time.sleep(.05)
                self.assertEqual(result['state'],'succeeded')
                self.assertEqual(result['answer'],'PROXY-MARKER')
                self.assertIn('REMOTE-EDIT',result['remote_artifacts']['diff'])
                self.assertEqual((repo/'file.txt').read_text(),'BASE')
                self.assertTrue(str(result['remote_artifacts']['workspace']).startswith(str(remote_root)))
                self.assertEqual(result['remote_task']['native_session_id'],'native-session')
            finally:controller.close()
        finally:
            server.shutdown();server.server_close();service.close();thread.join(2)
