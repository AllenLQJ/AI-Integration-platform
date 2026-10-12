import asyncio
import json
import os
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
                    self.assertEqual(len(names), 16)
                    self.assertIn('list_nodes', names)
                    self.assertIn('claim_controller', names)
                    self.assertIn('switch_executor', names)
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
