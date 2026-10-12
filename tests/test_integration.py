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


class IntegrationTests(unittest.TestCase):
    def test_daemon_autostart_singleton_restart(self):
        with tempfile.TemporaryDirectory() as td:
            client = Client(td)
            self.assertEqual(client.call('list_agents'), {})
            first = client.request('/health')['pid']
            self.assertEqual(Client(td).call('list_agents'), {})
            self.assertEqual(client.request('/health')['pid'], first)
            with self.assertRaises(HubError): InstanceLock(td)
            client.stop()
            for _ in range(100):
                if not (Path(td) / 'connection.json').exists(): break
                time.sleep(.05)
            time.sleep(.3)
            self.assertEqual(client.call('list_agents'), {})
            self.assertNotEqual(client.request('/health')['pid'], first)
            client.stop()
            for _ in range(100):
                if not (Path(td) / 'connection.json').exists(): break
                time.sleep(.05)
            time.sleep(.3)

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
                    self.assertEqual(len(names), 13)
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
            Client(td).stop()
            for _ in range(100):
                if not (Path(td) / 'connection.json').exists(): break
                await asyncio.sleep(.05)
            await asyncio.sleep(.3)
        with tempfile.TemporaryDirectory() as td:
            asyncio.run(check(td))
