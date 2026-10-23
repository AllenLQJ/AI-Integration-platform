"""An independently installable contract example; this is not an AI model."""
import time
import uuid

from agent_hub.models import HubError
from agent_hub.plugins import Executor, ExecutorPlugin


class Echo(Executor):
    capabilities = ['delegate', 'continue']
    remote_cancel = True
    cancel_scope = 'local_process_tree'

    def __init__(self, config, context):
        if set(config) - {'prefix'} or not isinstance(config.get('prefix', ''), str):
            raise HubError('INVALID_CONFIG', 'Echo supports only a string prefix option')
        self.prefix = config.get('prefix', '')
        self.context = context

    def diagnose(self):
        return {'available': True, 'version': '1.0.0', 'authentication': 'No account required; deterministic example'}

    def run(self, task, cancel, event):
        if cancel.is_set() or time.time() >= task['deadline']:
            raise HubError('CANCELLED' if cancel.is_set() else 'TIMED_OUT', 'Echo stopped')
        self.context.data_dir.mkdir(parents=True, exist_ok=True)
        event('progress', {'message': 'Example executor started'})
        return {'answer': self.prefix + task['prompt'],
                'session_id': task.get('resume_session') or uuid.uuid4().hex}


plugin = ExecutorPlugin(api_version=1, create=Echo)
