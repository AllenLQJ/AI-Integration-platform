import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from agent_hub.adapters.muse_media import images_for_task
from agent_hub.adapters.muse import Muse
from agent_hub.models import HubError

URL = 'https://7153a895-5c88-45e0-a9e8-8c0b71e299c6.metaaivm.com/media/raw/workspace/imagine_media/image.png?token=private'


class MuseMediaTests(unittest.TestCase):
    def row(self, **changes):
        image = dict(mime='image/png', byte_len=99, missing=False, path='sandbox://workspace/imagine_media/image.png',
                     variants={'original': URL})
        image.update(changes)
        return {'seq': 5, 'event_name': 'delta.presentation', 'raw': {'payload': {
            'kind': 'image', 'id': 'presentation', 'data': {'images': [image]}}}}

    def test_only_structured_task_images(self):
        source = {'seq': 4, 'event_name': 'delta.presentation', 'raw': {'payload': {
            'kind': 'external_link', 'data': {'url': URL}}}}
        self.assertEqual(images_for_task([source], 0), [])
        self.assertEqual(images_for_task([self.row()], 5), [])
        self.assertEqual(len(images_for_task([source, self.row()], 0)), 1)
        self.assertEqual(images_for_task([self.row(), {'seq':6,'role':'user'}], 0), [])

    def test_reject_missing_untrusted_and_invalid_sizes(self):
        for changes in ({'missing': True}, {'variants': {'original': 'http://127.0.0.1/a.png'}},
                        {'byte_len': True}, {'byte_len': 1000000000}, {'mime':'video/mp4'}):
            with self.subTest(changes=changes), self.assertRaises(HubError):
                images_for_task([self.row(**changes)], 0)

    def test_adapter_fetch_decodes_and_preserves_evidence(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            task = dict(id='task', media_artifact_root=str(Path(td)/'media'),
                        workspace=td, log_dir=str(Path(td)/'logs'), deadline=time.time()+10)
            Path(task['log_dir']).mkdir()
            images = images_for_task([self.row()], 0)
            images = [{k:v for k,v in i.items() if k != 'url'} for i in images]
            def transfer(argv, *args):
                data = json.loads(Path(argv[-1]).read_text())
                self.assertNotIn('https://', str(data))
                root = Path(data['root']);root.mkdir(exist_ok=True)
                Image.new('RGB',(2,3),'red').save(root/data['images'][0]['filename'])
                return 0, json.dumps({'hub_result':{'ok':True}}), '', {}
            with patch('agent_hub.adapters.muse.run_process', side_effect=transfer):
                result = Muse('configured-python').fetch_media(task, images, threading.Event(), lambda *a:None)
            self.assertEqual(result[0]['generation_evidence']['width'],2)
            self.assertEqual(result[0]['generation_evidence']['height'],3)
            self.assertNotIn('private',str(result))

    def test_adapter_transfer_failure_has_no_private_url(self):
        with tempfile.TemporaryDirectory() as td:
            task=dict(id='task',media_artifact_root=td,workspace=td,log_dir=td,deadline=time.time()+10)
            images=[{k:v for k,v in i.items() if k != 'url'} for i in images_for_task([self.row()],0)]
            with patch('agent_hub.adapters.muse.run_process',return_value=(0,'', '', {})):
                with self.assertRaises(HubError) as error:
                    Muse('python').fetch_media(task,images,threading.Event(),lambda *a:None)
            self.assertEqual(error.exception.code,'ARTIFACT_FETCH_FAILED')
            self.assertNotIn('private',str(error.exception))

    def test_explicit_model_and_video_are_rejected_before_send(self):
        for requested in ({'media_type':'image','model':'unknown'},{'media_type':'video','model':'muse:auto'}):
            with self.assertRaises(HubError):
                Muse('python').run({'context':{'payload':{'requested_output':requested}}},threading.Event(),lambda *a:None)
