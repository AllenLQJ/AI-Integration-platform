import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from agent_hub.daemon import make_server
from agent_hub.service import Service
from test_service import Fake


class DaemonTests(unittest.TestCase):
    def test_authenticated_short_rpc_and_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            service = Service(td, {'fake': Fake()})
            server = make_server(service, 'private-test-value')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = 'http://127.0.0.1:' + str(server.server_port)
            try:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url + '/health')
                self.assertEqual(error.exception.code, 401)
                request = urllib.request.Request(url + '/rpc', data=json.dumps({
                    'tool': 'list_agents', 'arguments': {}}).encode(),
                    headers={'Authorization': 'Bearer private-test-value', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
                self.assertTrue(result['result']['fake']['available'])
            finally:
                server.shutdown(); server.server_close(); thread.join(); service.close()

if __name__ == '__main__': unittest.main()
