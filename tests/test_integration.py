import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from agent_hub.client import Client
from agent_hub.daemon import InstanceLock
from agent_hub.models import HubError


def stop_and_wait(client):
    # connection.json removal precedes process exit; Windows still locks daemon.log.
    handle = None
    if os.name == 'nt':
        import win32api, win32con, win32event
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False,
                                      client.request('/health')['pid'])
    try:
        client.stop()
        if handle is not None:
            if win32event.WaitForSingleObject(handle, 10000) != win32event.WAIT_OBJECT_0:
                raise AssertionError('daemon did not exit after shutdown')
        else:
            for _ in range(100):
                if not (client.root / 'connection.json').exists():
                    break
                time.sleep(.05)
        if (client.root / 'connection.json').exists():
            raise AssertionError('daemon connection descriptor remains after shutdown')
    finally:
        if handle is not None:
            handle.Close()


class IntegrationTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'Windows starts the daemon through WMI')
    def test_detach_spawned_daemon_leaves_the_parent_session_and_fds(self):
        read_fd, write_fd = os.pipe()
        script = (
            'import os\n'
            'from agent_hub.client import detach_spawned_daemon\n'
            'extra = int(os.environ["EXTRA_FD"])\n'
            'parent = os.getsid(0)\n'
            'detach_spawned_daemon()\n'
            'try:\n'
            '    os.fstat(extra)\n'
            '    alive = True\n'
            'except OSError:\n'
            '    alive = False\n'
            'open(os.devnull, "rb").close()\n'
            'print(int(os.getsid(0) != parent), int(alive))\n'
        )
        env = dict(os.environ, EXTRA_FD=str(write_fd), AGENT_HUB_DETACH='1', PYTHONPATH='src')
        try:
            completed = subprocess.run(
                [sys.executable, '-c', script], env=env, input=b'',
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, close_fds=False)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines()[-1], b'1 0')

    @unittest.skipIf(os.name == 'nt', 'Windows starts the daemon through WMI')
    def test_darwin_daemon_spawn_avoids_fork(self):
        from unittest.mock import patch
        from agent_hub.client import Client
        with tempfile.TemporaryDirectory() as td:
            client = Client(td)
            with patch('agent_hub.client.sys.platform', 'darwin'), \
                    patch('agent_hub.client.subprocess.Popen', side_effect=RuntimeError('stop')) as popen, \
                    patch.object(client, 'request', side_effect=OSError):
                with self.assertRaises(RuntimeError):
                    client.ensure()
            kwargs = popen.call_args.kwargs
            self.assertFalse(kwargs['start_new_session'])
            self.assertFalse(kwargs['close_fds'])
            self.assertEqual(kwargs['env']['AGENT_HUB_DETACH'], '1')

    @unittest.skipIf(os.name == 'nt', 'Windows starts the daemon through WMI')
    def test_other_posix_daemon_spawn_starts_a_new_session(self):
        from unittest.mock import patch
        from agent_hub.client import Client
        with tempfile.TemporaryDirectory() as td:
            client = Client(td)
            with patch('agent_hub.client.sys.platform', 'linux'), \
                    patch('agent_hub.client.subprocess.Popen', side_effect=RuntimeError('stop')) as popen, \
                    patch.object(client, 'request', side_effect=OSError):
                with self.assertRaises(RuntimeError):
                    client.ensure()
            kwargs = popen.call_args.kwargs
            self.assertTrue(kwargs['start_new_session'])
            self.assertTrue(kwargs['close_fds'])
            self.assertNotIn('env', kwargs)

    def test_daemon_autostart_singleton_restart(self):
        with tempfile.TemporaryDirectory() as td:
            client = Client(td)
            self.assertEqual(client.call('list_agents'), {})
            first = client.request('/health')['pid']
            self.assertEqual(Client(td).call('list_agents'), {})
            self.assertEqual(client.request('/health')['pid'], first)
            with self.assertRaises(HubError): InstanceLock(td)
            stop_and_wait(client)
            self.assertEqual(client.call('list_agents'), {})
            self.assertNotEqual(client.request('/health')['pid'], first)
            stop_and_wait(client)

    def test_mcp_tools_over_stdio(self):
        async def check(td):
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            env = dict(os.environ, AGENT_HUB_DATA_DIR=td)
            parameters = StdioServerParameters(command=sys.executable, args=['-m', 'agent_hub', 'mcp'], env=env)
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    self.assertEqual(len(names), 20)
                    self.assertIn('list_nodes', names)
                    self.assertIn('claim_controller', names)
                    self.assertIn('switch_executor', names)
                    self.assertIn('record_activity', names)
                    self.assertNotIn('record_external_verification', names)
                    self.assertIn('verify_task', names)
                    self.assertIn('get_work_item', names)
                    self.assertNotIn('record_verification', names)
                    self.assertIn('resume_task', names)
                    self.assertIn('wait_for_task', names)
                    self.assertIn('continue_task', names)
                    response = await session.call_tool('list_agents', {})
                    self.assertFalse(response.isError)
            # Closing MCP frontend does not stop the independent daemon.
            self.assertTrue(Client(td).request('/health')['ok'])
            await asyncio.to_thread(stop_and_wait, Client(td))
        with tempfile.TemporaryDirectory() as td:
            asyncio.run(check(td))
