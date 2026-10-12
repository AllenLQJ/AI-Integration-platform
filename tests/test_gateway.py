import json
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from agent_hub.models import HubError
from agent_hub.service import Service
from test_service import Fake


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = Fake()
        self.service = Service(self.root / 'data', {'fake': self.fake}, per_agent_concurrency={'fake': 0})

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def submit(self, context=None):
        return self.service.call('submit_task', dict(request_id='same', agent='fake', prompt='原始 instruction',
            cwd=str(self.root), mode='read_only', context=context))

    def test_context_idempotence_and_persistence(self):
        context = {'contract_version': '1.0', 'correlation': {'attempt': 1}, 'payload': {'attachments': []}}
        task = self.submit(context)
        self.assertEqual(task['contract_version'], '1.0')
        self.assertEqual(self.submit(json.loads(json.dumps(context)))['id'], task['id'])
        self.assertEqual(self.service.store.get(task['id'])['context'], context)
        context['correlation']['attempt'] = 2
        with self.assertRaises(HubError) as error:
            self.submit(context)
        self.assertEqual(error.exception.code, 'REQUEST_CONFLICT')
        self.assertEqual(self.fake.calls, 0)

    def test_old_submit_and_unsupported_attachments(self):
        self.assertEqual(self.submit()['id'], self.submit()['id'])
        with self.assertRaises(HubError) as error:
            self.submit({'contract_version': '1.0', 'payload': {'attachments': [{'path': '/secret'}]}})
        self.assertEqual(error.exception.code, 'UNSUPPORTED_INPUT_TRANSPORT')

    def test_capabilities_are_read_only_and_require_evidence(self):
        result = self.service.call('get_capabilities', {})
        self.assertEqual(result['agents']['fake']['capabilities'], [])
        self.assertEqual(result['service_version'], '0.2.1')
        self.assertEqual(self.fake.calls, 0)
        item = dict(kind='image.generate', model='actual-model', verification='unverified')
        self.service.gateway.config = {'agents': {'fake': {'media_capabilities': [item]}}}
        self.assertEqual(self.service.call('get_capabilities', {})['agents']['fake']['capabilities'][0]['verification'], 'unverified')
        item['verification'] = 'verified'
        with self.assertRaises(HubError):
            self.service.call('get_capabilities', {})

    def image(self):
        folder = self.service.gateway.artifact_root
        folder.mkdir(exist_ok=True)
        path = folder / '真实.png'
        Image.new('RGB', (2, 2), 'red').save(path)
        return path

    def test_stable_authorized_artifact_and_tampering(self):
        task = self.submit()
        path = self.image()
        entries = [{'path': str(path), 'mime_type': 'image/png'}]
        metadata = self.service.gateway.register(task['id'], entries)[0]
        self.assertEqual(metadata, self.service.gateway.register(task['id'], entries)[0])
        response = self.service.call('get_artifact', {'artifact_id': metadata['artifact_id']})
        self.assertEqual(response['artifact'], metadata)
        self.assertEqual(response['transfer']['host_id'], self.service.call('get_capabilities', {})['host_id'])
        from agent_hub.gateway import Gateway
        restored = Gateway(self.service.root, self.service.store)
        self.assertEqual(restored.artifact(metadata['artifact_id']), response)
        path.write_bytes(b'changed')
        with self.assertRaises(HubError) as error:
            restored.artifact(metadata['artifact_id'])
        self.assertEqual(error.exception.code, 'ARTIFACT_EXPIRED')
        path.unlink()
        with self.assertRaises(HubError) as error:
            restored.artifact(metadata['artifact_id'])
        self.assertEqual(error.exception.code, 'ARTIFACT_NOT_FOUND')

    def test_no_escape_or_invalid_image(self):
        path = self.image()
        outside = self.root / 'outside.png'
        outside.write_bytes(path.read_bytes())
        with self.assertRaises(HubError):
            self.service.gateway.register('task', [{'path': str(outside)}])
        path.write_bytes(b'\x89PNG\r\n\x1a\ninvalid')
        with self.assertRaises(HubError) as error:
            self.service.gateway.register('task', [{'path': str(path)}])
        self.assertEqual(error.exception.code, 'INVALID_ARTIFACT')

    def test_no_symlink_escape(self):
        path = self.image()
        outside = self.root / 'outside.png'
        outside.write_bytes(path.read_bytes())
        link = path.parent / 'link.png'
        try:
            link.symlink_to(outside)
        except OSError as error:
            if getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows account lacks symbolic-link privilege')
            raise
        with self.assertRaises(HubError):
            self.service.gateway.register('task', [{'path': str(link)}])

    def test_states_and_source_urls(self):
        task = self.submit()
        self.service.store.finish(task['id'], 'delivery_unknown', {'source_urls': ['https://example.com/result.png']})
        result = self.service.call('get_result', {'task_id': task['id']})
        self.assertEqual(result['state'], 'delivery_unknown')
        self.assertTrue(result['execution_uncertain'])
        self.assertEqual(result['media_artifacts'], [])
        self.assertEqual(result['contract_version'], '1.0')

    def test_actual_adapter_registration(self):
        path = self.image()
        def run(task, cancel, event):
            return {'answer': '{"review":"原文"}', 'media_artifacts': [{'path': str(path)}]}
        self.fake.run = run
        self.service.per_agent_concurrency['fake'] = 1
        task = self.submit()
        self.service.wait_for_task(task['id'], timeout_seconds=3)
        result = self.service.call('get_result', {'task_id': task['id']})
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['answer'], '{"review":"原文"}')
        artifact = result['media_artifacts'][0]
        self.assertEqual(artifact['task_id'], task['id'])
        self.service.call('get_artifact', {'artifact_id': artifact['artifact_id']})
