import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent_hub.daemon import make_server
from agent_hub.service import Service
from test_service import Fake


class WaitMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_wait_does_not_block_query_and_matches_http_contract(self):
        with tempfile.TemporaryDirectory() as td:
            service = Service(td, {'fake': Fake()})
            server = make_server(service, 'test-private-value')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            Path(td, 'connection.json').write_text(json.dumps({
                'url': 'http://127.0.0.1:' + str(server.server_port), 'token': 'test-private-value'}))
            params = StdioServerParameters(command=sys.executable, args=['-m', 'agent_hub', 'mcp'],
                env=dict(os.environ, AGENT_HUB_DATA_DIR=td, PYTHONUTF8='1'))
            try:
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = {t.name: t for t in (await session.list_tools()).tools}
                        self.assertIn('wait_for_task', tools)
                        self.assertIn('get_capabilities', tools)
                        self.assertIn('get_artifact', tools)
                        self.assertIn('context', tools['submit_task'].inputSchema['properties'])
                        self.assertIn('work_item_id', tools['submit_task'].inputSchema['properties'])
                        caps = json.loads((await session.call_tool('get_capabilities', {})).content[0].text)
                        self.assertEqual(caps['contract_version'], '1.0')
                        missing = json.loads((await session.call_tool('get_artifact', {'artifact_id': 'missing'})).content[0].text)
                        self.assertFalse(missing['error']['retryable'])
                        self.assertEqual(missing['error']['code'], 'ARTIFACT_NOT_FOUND')
                        self.assertTrue(tools['wait_for_task'].annotations.readOnlyHint)
                        work_item = json.loads((await session.call_tool('create_work_item', dict(
                            request_id='wi:mcp-wait', title='mcp wait'))).content[0].text)
                        created = await session.call_tool('submit_task', dict(request_id='mcp-wait',
                            agent='fake', prompt='wait', cwd=td, mode='read_only',
                            work_item_id=work_item['work_item_id']))
                        task = json.loads(created.content[0].text)
                        waiting = asyncio.create_task(session.call_tool('wait_for_task',
                            dict(task_id=task['id'], timeout_seconds=1)))
                        await asyncio.sleep(.15)
                        self.assertFalse(waiting.done())
                        start = time.monotonic()
                        query = await session.call_tool('get_task', {'task_id': task['id']})
                        self.assertLess(time.monotonic() - start, .6)
                        self.assertEqual(json.loads(query.content[0].text)['state'], 'running')
                        timeout = json.loads((await waiting).content[0].text)
                        self.assertTrue(timeout['timed_out'])
                        self.assertEqual(timeout['state'], 'running')
                        await session.call_tool('cancel_task', {'task_id': task['id']})
                        final = await session.call_tool('wait_for_task', dict(task_id=task['id'],
                            after_seq=timeout['next_seq'], timeout_seconds=2))
                        response = json.loads(final.content[0].text)
                        self.assertEqual(response['state'], 'cancelled')
                        self.assertEqual(response, service.call('wait_for_task', dict(task_id=task['id'],
                            after_seq=timeout['next_seq'], timeout_seconds=0)))
                        error = await session.call_tool('wait_for_task', {'task_id': 'missing'})
                        self.assertEqual(json.loads(error.content[0].text)['error']['code'], 'TASK_NOT_FOUND')
                        for bad in ({'timeout_seconds': True}, {'after_seq': True}):
                            with self.subTest(bad=bad):
                                rejected = await session.call_tool('wait_for_task', dict(task_id=task['id'], **bad))
                                payload = json.loads(rejected.content[0].text) if not rejected.isError else {}
                                self.assertTrue(rejected.isError or payload.get('error', {}).get('code') == 'INVALID_ARGUMENT')
            finally:
                await asyncio.to_thread(server.shutdown)
                server.server_close(); thread.join(2); service.close()


if __name__ == '__main__':
    unittest.main()
