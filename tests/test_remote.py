import base64
import io
import os
import re
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
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([],0,b'{"ok":true}',b'')) as run:
            self.assertEqual(client.call('submit_task', {'prompt':'$(unsafe)`cmd`'}), {'ok':True})
        argv = run.call_args.args[0]
        self.assertEqual(shlex.split(argv[-1])[0], '/space dir/python')
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertNotIn('unsafe', str(argv))
        self.assertIn(b'unsafe', run.call_args.kwargs['input'])

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
        with patch('subprocess.run',return_value=subprocess.CompletedProcess([],1,b'{"error":{"code":[],"message":{}}}',b'')):
            with self.assertRaises(HubError) as error:
                SshClient('host','/python','/data').call('get_task',{})
        self.assertEqual(error.exception.code,'INVALID_RESPONSE')

    def test_utf8_bytes_roundtrip_with_peer_using_gbk_stdio(self):
        real_run = subprocess.run
        script = ('import sys,json; '
                  'assert sys.stdin.encoding.lower() == "gbk", sys.stdin.encoding; '
                  'assert sys.stdout.encoding.lower() == "gbk", sys.stdout.encoding; '
                  'p=json.loads(sys.stdin.buffer.read().decode("utf-8")); '
                  'sys.stdout.buffer.write(json.dumps(p,ensure_ascii=False).encode("utf-8"))')
        def local_transport(argv, **kwargs):
            # A real subprocess has GBK text streams. The RPC deliberately uses
            # buffer bytes, so locale must not affect non-GBK characters either.
            env = dict(os.environ, PYTHONIOENCODING='gbk', PYTHONUTF8='0')
            return real_run([sys.executable, '-X', 'utf8=0', '-c', script], env=env, **kwargs)
        payload = {'prompt': '你好 🧪', 'cwd': '/Users/your-name/项目'}
        with patch('subprocess.run', side_effect=local_transport):
            self.assertEqual(SshClient('host','/python','/data').call('submit_task',payload),payload)

    def test_failed_task_result_is_not_rpc_error(self):
        for state in ('failed', 'timed_out', 'cancelled', 'interrupted', 'delivery_unknown'):
            with self.subTest(state=state):
                result = {'ready': True, 'state': state, 'error': '本地执行进程树已停止',
                          'error_code': 'CANCELLED', 'errors': [{'code': 'TASK_TIMED_OUT'}]}
                with patch('subprocess.run', return_value=subprocess.CompletedProcess(
                        [],0,json.dumps(result).encode('utf-8'),b'')):
                    self.assertEqual(SshClient('host','/python','/data').call('get_result',{}),result)

    def test_rpc_error_still_raises_original_code_and_message(self):
        response = {'error': {'code': 'UNKNOWN_TASK', 'message': '任务不存在'}}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess(
                [],1,json.dumps(response).encode('utf-8'),b'')):
            with self.assertRaises(HubError) as error:
                SshClient('host','/python','/data').call('get_task',{})
        self.assertEqual(error.exception.code,'UNKNOWN_TASK')
        self.assertEqual(str(error.exception),'任务不存在')

    def test_nonzero_exit_cannot_be_returned_as_task_result(self):
        response = {'ready': True, 'state': 'failed', 'error': 'task failed'}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess(
                [],1,json.dumps(response).encode('utf-8'),b'')):
            with self.assertRaises(HubError) as error:
                SshClient('host','/python','/data').call('get_result',{})
        self.assertEqual(error.exception.code,'INVALID_RESPONSE')

    def test_invalid_utf8_response_preserves_no_retry_semantics(self):
        for tool, code in (('submit_task', 'DELIVERY_UNKNOWN'),
                           ('continue_task', 'DELIVERY_UNKNOWN'),
                           ('get_result', 'REMOTE_UNAVAILABLE')):
            with self.subTest(tool=tool), patch('subprocess.run', return_value=
                    subprocess.CompletedProcess([],0,b'\xff',b'')) as run:
                with self.assertRaises(HubError) as error:
                    SshClient('host','/python','/data').call(tool,{})
                self.assertEqual(error.exception.code,code)
                self.assertEqual(run.call_count,1)

    def test_nonzero_exit_without_rpc_error_is_unavailable(self):
        with patch('subprocess.run', return_value=
                subprocess.CompletedProcess([],1,b'{"ready":true,"state":"succeeded"}',b'')):
            with self.assertRaises(HubError) as error:
                SshClient('host','/python','/data').call('get_result',{})
        self.assertEqual(error.exception.code,'REMOTE_UNAVAILABLE')

    def test_uncertain_nonzero_delivery_never_claims_not_sent(self):
        for tool in ('submit_task', 'continue_task'):
            for body in (b'{"id":"accepted-task"}', b'{"error":"not an RPC envelope"}'):
                with self.subTest(tool=tool, body=body), patch('subprocess.run', return_value=
                        subprocess.CompletedProcess([],1,body,b'')) as run:
                    with self.assertRaises(HubError) as error:
                        SshClient('host','/python','/data').call(tool,{})
                    self.assertEqual(error.exception.code,'DELIVERY_UNKNOWN')
                    self.assertEqual(run.call_count,1)

    def test_remote_rpc_python_is_forced_to_utf8(self):
        with patch('subprocess.run', return_value=
                subprocess.CompletedProcess([],0,b'{}',b'')) as run:
            SshClient('host','/python','/data').call('list_agents')
        command = shlex.split(run.call_args.args[0][-1])
        self.assertEqual(command[:5],['/python','-X','utf8','-m','agent_hub'])


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

    def test_invalid_rpc_response_keeps_remote_task_binding(self):
        with patch.object(self.adapter.client,'call',side_effect=[
                {'id':'original'},HubError('INVALID_RESPONSE','bad response')]) as call:
            with self.assertRaises(HubError) as error:
                self.adapter.run(self.task,threading.Event(),lambda *e:self.events.append(e))
        self.assertEqual(error.exception.code,'REMOTE_STATE_UNKNOWN')
        self.assertEqual(error.exception.details['remote_task']['task_id'],'original')
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
            controller_root = self.root / 'controller'
            controller_root.mkdir()
            (controller_root / 'config.json').write_text(json.dumps({'agents': {'target-worker': {'platform': 'darwin'}}}))
            controller=Service(controller_root,{'target-worker':proxy})
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


class WindowsSshTests(unittest.TestCase):
    def test_native_absolute_paths_and_boundaries(self):
        from agent_hub.ssh_client import validate_remote_path
        valid = [r'C:\Python\python.exe', 'D:/中文 空格/目录',
                 r"C:\单'引号 & $();` %!^\python.exe", r'\\server\share',
                 r'\\server\share\中文 空格', '//server/share/path']
        invalid = ['python.exe', r'C:relative', r'\rooted', '/posix',
                   r'\\server', '\\\\server\\', r'\\?\C:\data', r'\\.\pipe\x',
                   r'\\..\share', 'C:/bad:stream', 'C:/a"b', 'C:/a|b',
                   'C:/a*b', 'C:/a?b', 'C:/a\x00b', 'C:/a\nb', 'C:/a\rb',
                   'C:/a\tb', 'C:/a\x7fb', 'C:/a\x85b']
        for path in valid:
            with self.subTest(path=path):
                self.assertEqual(validate_remote_path(path, 'windows'), path)
                Remote('host', path, path, 'worker', path, 'read_only', platform='windows')
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(HubError):
                validate_remote_path(path, 'windows')
        for slot in ('python', 'data_dir', 'cwd'):
            values = dict(python='C:/python.exe', data_dir='C:/data', cwd='C:/work')
            values[slot] = 'C:relative'
            with self.subTest(slot=slot), self.assertRaises(HubError):
                Remote('host', values['python'], values['data_dir'], 'worker',
                       values['cwd'], 'read_only', platform='windows')

    def test_encoded_script_quotes_paths_and_never_contains_payload(self):
        import base64
        python = "C:/中文 单'引号 & $();` %!^/python.exe"
        root = "//server/share/目录 单'引号 & $();` %!^"
        payload = {'prompt': '唯一秘密 中文 🧪\r\n$(unsafe)`cmd`', 'cwd': 'C:/工作'}
        for shell in ('cmd', 'powershell'):
            with self.subTest(shell=shell), patch('subprocess.run', return_value=
                    subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
                SshClient('host', python, root, platform='windows', shell=shell).call('submit_task', payload)
                argv = run.call_args.args[0]
                command = argv[-1]
                encoded = re.search(r'-EncodedCommand ([A-Za-z0-9+/=]+)', command).group(1)
                script = base64.b64decode(encoded).decode('utf-16le')
                self.assertIn("$s.FileName = '" + python.replace("'", "''") + "';", script)
                self.assertIn(root.replace("'", "''"), script)
                self.assertIn('--arguments -', script)
                self.assertIn('$code = $p.ExitCode', script)
                self.assertIn('exit $code', script)
                self.assertIn('$s.RedirectStandardInput = $false', script)
                self.assertIn('StandardOutput.BaseStream.CopyToAsync', script)
                self.assertIn('StandardError.BaseStream.CopyToAsync', script)
                self.assertNotIn('InputFormat', command)
                self.assertNotIn('|', script)
                self.assertNotIn('唯一秘密', script + command)
                self.assertEqual(run.call_args.kwargs['input'], json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                self.assertIn('StrictHostKeyChecking=yes', argv)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(command.startswith('try {'), shell == 'powershell')

    def test_all_powershell_quote_characters_in_paths_and_attacks(self):
        from agent_hub.ssh_client import powershell_literal
        for quote in "'‘’‚‛":
            normal = 'C:/正常' + quote + '目录/python.exe'
            attack = 'C:/x' + quote + '; Write-Output INJECTED; #' + quote + '/python.exe'
            for path in (normal, attack):
                with self.subTest(quote=hex(ord(quote)), path=path):
                    expected = "'" + path.replace(quote, quote * 2) + "'"
                    self.assertEqual(powershell_literal(path), expected)
                    for shell in ('cmd', 'powershell'):
                        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
                            SshClient('host', path, path, platform='windows', shell=shell).call('list_agents')
                        command = run.call_args.args[0][-1]
                        encoded = re.search(r'-EncodedCommand ([A-Za-z0-9+/=]+)', command).group(1)
                        script = base64.b64decode(encoded).decode('utf-16le')
                        self.assertIn('$s.FileName = ' + expected + '; $s.Arguments = ', script)
                        self.assertIn(path.replace(quote, quote * 2), script)
        self.assertEqual(powershell_literal("'‘’‚‛"), "'''‘‘’’‚‚‛‛'")

    def test_mcp_invalid_target_config_exits_cleanly_on_stderr(self):
        from agent_hub.__main__ import main
        for python, shell in [('relative-python', 'cmd'), ('C:/python.exe', 'posix')]:
            argv = ['agent_hub', 'mcp', '--ssh-host', 'host', '--remote-python', python,
                    '--remote-data-dir', 'C:/data', '--remote-platform', 'windows', '--remote-shell', shell]
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(python=python, shell=shell), patch.object(sys, 'argv', argv), \
                    patch('sys.stdout', stdout), patch('sys.stderr', stderr), \
                    patch('agent_hub.mcp_server.run') as run, self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 1)
            run.assert_not_called()
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(json.loads(stderr.getvalue())['error']['code'], 'INVALID_CONFIG')
            self.assertNotIn('Traceback', stderr.getvalue())

    def test_remote_config_requires_concrete_node_platform(self):
        from agent_hub.config import adapters, remote_node_declaration
        for platform in (None, 'posix', 'linux', 'darwin', 'windows'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                path = 'C:/target' if platform == 'windows' else '/target'
                item = dict(kind='remote', ssh_host='host', python=path,
                            data_dir=path, cwd=path, agent='worker', mode='read_only')
                if platform is not None: item['platform'] = platform
                (Path(directory) / 'config.json').write_text(json.dumps({'agents': {'target': item}}))
                if platform in (None, 'posix'):
                    with self.assertRaises(HubError) as error: adapters(directory)
                    self.assertEqual(error.exception.code, 'INVALID_CONFIG')
                    with self.assertRaises(HubError): remote_node_declaration('target', item, 'host')
                else:
                    adapter = adapters(directory)['target']
                    self.assertEqual(adapter.client.platform, remote_node_declaration('target', item, 'host')[1])

    def test_platform_shell_rejects_unsupported_pairs(self):
        for platform, shell in [('windows', 'bash'), ('linux', 'cmd'), ('posix', 'powershell'),
                                ('auto', None), (None, None), ('windows', ''), ([], None), ('windows', {})]:
            with self.subTest(platform=platform, shell=shell), self.assertRaises(HubError):
                SshClient('host', '/python', '/data', platform=platform, shell=shell)
        self.assertEqual(SshClient('host', 'C:/python.exe', 'C:/data', platform='windows').shell, 'cmd')
        for platform in ('posix', 'linux', 'darwin'):
            self.assertEqual(SshClient('host', '/python', '/data', platform=platform).shell, 'posix')

    def test_config_passes_target_settings_and_cwd(self):
        from agent_hub.config import adapters
        with tempfile.TemporaryDirectory() as directory:
            item = dict(kind='remote', ssh_host='host', python='C:/python.exe',
                        data_dir='C:/data', cwd='C:/中文 work', agent='worker',
                        mode='read_only', platform='windows', shell='powershell')
            (Path(directory) / 'config.json').write_text(json.dumps({'agents': {'win': item}}))
            adapter = adapters(directory)['win']
            self.assertEqual((adapter.client.platform, adapter.client.shell, adapter.cwd),
                             ('windows', 'powershell', item['cwd']))
            with patch.object(adapter.client, 'call', side_effect=[{'id': 'remote'},
                    {'ready': True, 'state': 'succeeded', 'answer': 'ok'}]) as call:
                adapter.run(dict(id='local', prompt='secret', deadline=time.time()+10,
                                 log_dir=directory), threading.Event(), lambda *args: None)
            self.assertEqual(call.call_args_list[0].args[1]['cwd'], item['cwd'])

    def test_all_cli_entrypoints_pass_platform_and_shell(self):
        from agent_hub.__main__ import main
        for command in ('status', 'rpc', 'show', 'mcp'):
            argv = ['agent_hub', command]
            if command == 'rpc': argv += ['list_agents']
            if command == 'show': argv += ['task-id']
            argv += ['--ssh-host', 'host', '--remote-python', 'C:/python.exe',
                     '--remote-data-dir', 'C:/data', '--remote-platform', 'windows',
                     '--remote-shell', 'powershell']
            with self.subTest(command=command), patch.object(sys, 'argv', argv), \
                    patch('agent_hub.ssh_client.SshClient') as client, \
                    patch('agent_hub.mcp_server.run') as mcp, \
                    patch('agent_hub.verification.render', return_value='rendered'), \
                    patch('sys.stdout'):
                client.return_value.call.return_value = {}
                main()
                client.assert_called_once_with('host', 'C:/python.exe', 'C:/data',
                                               platform='windows', shell='powershell')
                if command == 'mcp': mcp.assert_called_once_with(client.return_value)

    def test_cli_settings_require_ssh_host(self):
        from agent_hub.__main__ import main
        for option, value in [('--remote-platform', 'windows'), ('--remote-shell', 'cmd')]:
            with patch.object(sys, 'argv', ['agent_hub', 'status', option, value]), \
                    patch('sys.stderr'), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 2)

    def test_backslash_directory_root_uses_crt_escaping(self):
        for root in ('C:\\中文 空格\\', '\\\\server\\share\\中文 空格\\'):
            with self.subTest(root=root), patch('subprocess.run', return_value=
                    subprocess.CompletedProcess([], 0, b'{}', b'')) as run:
                SshClient('host', 'C:/python.exe', root, platform='windows').call('list_agents')
                script = base64.b64decode(run.call_args.args[0][-1].split()[-1]).decode('utf-16le')
                # CRT quoting doubles the final backslash before the closing quote.
                self.assertIn('--data-dir "' + root + '\\"', script)

    def test_windows_command_length_fails_before_transport(self):
        client = SshClient('host', 'C:/python.exe', 'C:/' + 'a' * 4000, platform='windows')
        with patch('subprocess.run') as run, self.assertRaises(HubError) as error:
            client.call('submit_task', {'prompt': 'secret'})
        self.assertEqual(error.exception.code, 'INVALID_CONFIG')
        run.assert_not_called()

    def test_posix_paths_reject_controls_in_all_fields(self):
        for path in ('/data\tbad', '/data\x85bad'):
            with self.subTest(path=path), self.assertRaises(HubError):
                Remote('host', '/python', '/data', 'worker', path, 'read_only')

    def test_old_cli_uses_posix_defaults(self):
        from agent_hub.__main__ import main
        argv = ['agent_hub', 'status', '--ssh-host', 'host',
                '--remote-python', '/python', '--remote-data-dir', '/data']
        with patch.object(sys, 'argv', argv), patch('agent_hub.ssh_client.SshClient') as client, patch('sys.stdout'):
            client.return_value.call.return_value = {}
            main()
        client.assert_called_once_with('host', '/python', '/data', platform='posix', shell=None)

    def test_windows_errors_and_no_retry(self):
        client = SshClient('host', 'C:/python.exe', 'C:/data', platform='windows')
        for tool, result, code in [
            ('submit_task', subprocess.TimeoutExpired('ssh', 1), 'DELIVERY_UNKNOWN'),
            ('continue_task', subprocess.TimeoutExpired('ssh', 1), 'DELIVERY_UNKNOWN'),
            ('get_result', subprocess.CompletedProcess([], 1, b'{"error":{"code":"UNKNOWN_TASK","message":"gone"}}', b''), 'UNKNOWN_TASK'),
            ('get_result', subprocess.CompletedProcess([], 1, b'{}', b''), 'REMOTE_UNAVAILABLE')]:
            kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
            with self.subTest(tool=tool), patch('subprocess.run', **kwargs) as run, self.assertRaises(HubError) as error:
                client.call(tool)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(run.call_count, 1)
        result = {'ready': True, 'state': 'failed', 'error': 'task failed'}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(result).encode(), b'')):
            self.assertEqual(client.call('get_result'), result)

    @unittest.skipUnless(sys.platform == 'win32', '需要真实 Windows cmd.exe / Windows PowerShell；未做 Windows 实测')
    def test_real_default_shells_inherit_raw_stdin_and_preserve_exit(self):
        import base64
        import shutil
        import venv
        from agent_hub.ssh_client import powershell_command
        if not shutil.which('powershell.exe'):
            self.skipTest('本机无 Windows PowerShell，未做 Windows 实测')
        payload = json.dumps({'prompt': ('中文 🧪 单\'引号 & %!^ $()`\r\n' * 5000)}, ensure_ascii=False).encode('utf-8')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "中文 单'‘’‚‛引号 & $();` %!^ ’; Write-Output INJECTED; #"
            fixture.mkdir()
            venv.EnvBuilder(with_pip=False).create(fixture / 'venv')
            python = str(fixture / 'venv' / 'Scripts' / 'python.exe')
            script = fixture / 'peer.py'
            script.write_text('import sys,json,base64\n'
                              'raw=sys.stdin.buffer.read()\n'
                              'print(json.dumps({"raw":base64.b64encode(raw).decode(),"args":sys.argv[1:]},ensure_ascii=False))\n'
                              'sys.stderr.buffer.write("中文 warning\\n".encode("utf-8"))\n'
                              'sys.exit(int(sys.argv[2]))\n', encoding='utf-8')
            for shell in ('cmd', 'powershell'):
                for code in (0, 37):
                    command = powershell_command([python, '-X', 'utf8', str(script), str(fixture), str(code)], shell)
                    # Match sshd's default shell startup, including AutoRun/profile.
                    launcher = (['cmd.exe', '/c', command] if shell == 'cmd' else
                                ['powershell.exe', '-c', command])
                    with self.subTest(shell=shell, exit_code=code):
                        # Real user profiles and first-use module initialization
                        # can take longer than 15 seconds on Windows.
                        result = subprocess.run(launcher, input=payload, capture_output=True, timeout=60)
                        self.assertEqual(result.returncode, code, result.stderr)
                        response = json.loads(result.stdout.decode('utf-8'))
                        self.assertEqual(base64.b64decode(response['raw']), payload)
                        self.assertEqual(response['args'], [str(fixture), str(code)])
                        self.assertEqual(result.stderr, '中文 warning\n'.encode('utf-8'))
            # Compare escaping with Windows PowerShell's public API and parser.
            from agent_hub.ssh_client import powershell_literal
            for value in (str(fixture), "C:/正常'‘’‚‛目录", "C:/x’; Write-Output INJECTED; #"):
                command = ("$v = " + powershell_literal(value) + "; "
                           "$escaped = [System.Management.Automation.Language.CodeGeneration]::"
                           "EscapeSingleQuotedStringContent($v); "
                           "$bytes = [Text.Encoding]::UTF8.GetBytes($v); "
                           "[Console]::WriteLine([Convert]::ToBase64String($bytes)); "
                           "[Console]::WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($escaped)))")
                result = subprocess.run(['powershell.exe', '-c', command], input=b'',
                                        capture_output=True, timeout=60, check=True)
                lines = result.stdout.splitlines()
                self.assertEqual(len(lines), 2, result.stdout)
                self.assertEqual(base64.b64decode(lines[0]).decode('utf-8'), value)
                self.assertEqual(base64.b64decode(lines[1]).decode('utf-8'), powershell_literal(value)[1:-1])
