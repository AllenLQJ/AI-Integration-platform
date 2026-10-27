import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from .config import data_dir, private_dir
from .models import HubError


class Client:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(self, path, body=None, timeout=15):
        connection = json.loads((self.root / 'connection.json').read_text(encoding='utf-8'))
        url = connection['url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise HubError('INVALID_CONNECTION', '服务地址必须为回环地址')
        request = urllib.request.Request(url + path,
                         data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
                         headers={'Authorization': 'Bearer ' + connection['token'], 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=timeout) as response: return json.load(response)
    def ensure(self):
        try:
            if self.request('/health').get('ok'): return
        except (OSError, ValueError): pass
        private_dir(self.root)
        argv = [sys.executable, '-m', 'agent_hub', 'daemon', '--data-dir', str(self.root)]
        if os.name == 'nt':
            # WMI creates the daemon outside the MCP client's kill-on-close Job Object.
            # DETACHED_PROCESS alone still inherits that job and is insufficient.
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                wmi = win32com.client.GetObject('winmgmts:root/cimv2')
                startup = wmi.Get('Win32_ProcessStartup').SpawnInstance_()
                startup.ShowWindow = 0
                process_class = wmi.Get('Win32_Process')
                parameters = process_class.Methods_('Create').InParameters.SpawnInstance_()
                parameters.Properties_('CommandLine').Value = subprocess.list2cmdline(argv)
                parameters.Properties_('ProcessStartupInformation').Value = startup
                response = wmi.ExecMethod('Win32_Process', 'Create', parameters)
                return_code = response.Properties_('ReturnValue').Value
                if return_code:
                    raise HubError('DAEMON_UNAVAILABLE', 'Windows 后台进程创建失败')
                response = parameters = process_class = startup = wmi = None
            finally: pythoncom.CoUninitialize()
        else:
            log = (self.root / 'daemon.log').open('ab')
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True, close_fds=True)
                # Retain the handle while this frontend lives; daemon itself outlives it.
                self._process = process
            finally: log.close()
        for _ in range(100):
            time.sleep(.1)
            try:
                if self.request('/health').get('ok'): return
            except (OSError, ValueError): pass
        message = '后台服务未启动，请检查私有目录中的 daemon.log'
        try: tail = (self.root / 'daemon.log').read_bytes()[-2000:].decode('utf-8', 'replace').strip()
        except OSError: tail = ''
        if tail: message += '\n--- daemon.log (末尾) ---\n' + tail
        raise HubError('DAEMON_UNAVAILABLE', message)
    def call(self, tool, arguments=None):
        self.ensure()
        # Mutating calls are never automatically retried after uncertain transport errors.
        # reconcile_task probe deadline is up to 24s; HTTP timeout must exceed the service bound.
        timeout = 45 if tool == 'reconcile_task' else 15
        response = self.request('/rpc', {'tool': tool, 'arguments': arguments or {}}, timeout=timeout)
        if 'error' in response:
            raise HubError(response['error']['code'], response['error']['message'], response['error'].get('details'))
        return response['result']
    def stop(self):
        return self.request('/shutdown', {})

    def create_work_item(self, request_id, title='工作项', acceptance=None):
        """Create a stable collaboration container; prefer this before dispatch.

        acceptance templates: software_dashboard_acceptance() / conversation_acceptance()
        from agent_hub.work_items (tests+ui+review content_review, or policy=no_extra).
        """
        args = dict(request_id=request_id, title=title)
        if acceptance is not None:
            args['acceptance'] = acceptance
        return self.call('create_work_item', args)

    def work_item(self, work_item_id, controller_id=None, controller_epoch=None):
        """Bind work_item_id so dispatch/review/rework always carry the same container."""
        return WorkItemClient(self, work_item_id, controller_id=controller_id, controller_epoch=controller_epoch)

    @staticmethod
    def software_dashboard_acceptance():
        from .work_items import software_dashboard_acceptance
        return software_dashboard_acceptance()

    @staticmethod
    def conversation_acceptance():
        from .work_items import conversation_acceptance
        return conversation_acceptance()


class WorkItemClient:
    """Controller helper: bind work_item_id; never infers chat identity."""

    def __init__(self, client, work_item_id, controller_id=None, controller_epoch=None):
        if not isinstance(work_item_id, str) or not work_item_id:
            raise HubError('INVALID_ARGUMENT', 'work_item_id 需要非空字符串')
        self.client = client
        self.work_item_id = work_item_id
        self.controller_id = controller_id
        self.controller_epoch = controller_epoch

    def _lease(self, arguments):
        args = dict(arguments)
        if self.controller_id is not None:
            args['controller_id'] = self.controller_id
        if self.controller_epoch is not None:
            args['controller_epoch'] = self.controller_epoch
        return args

    def dispatch(self, request_id, agent, prompt, cwd, mode, timeout_seconds=1800,
                 relation='development', related_task_id=None, **extra):
        args = dict(request_id=request_id, agent=agent, prompt=prompt, cwd=cwd, mode=mode,
                    timeout_seconds=timeout_seconds, work_item_id=self.work_item_id,
                    relation=relation, **extra)
        if related_task_id is not None:
            args['related_task_id'] = related_task_id
        return self.client.call('submit_task', args)

    def review(self, request_id, agent, prompt, cwd, related_task_id, mode='text_only',
               timeout_seconds=1800, **extra):
        return self.dispatch(request_id, agent, prompt, cwd, mode, timeout_seconds,
                             relation='review', related_task_id=related_task_id, **extra)

    def rework(self, request_id, agent, prompt, cwd, related_task_id, mode='edit_worktree',
               timeout_seconds=1800, **extra):
        return self.dispatch(request_id, agent, prompt, cwd, mode, timeout_seconds,
                             relation='rework', related_task_id=related_task_id, **extra)

    def link(self, request_id, task_id, relation='linked', related_task_id=None):
        args = self._lease(dict(request_id=request_id, work_item_id=self.work_item_id,
                                task_id=task_id, relation=relation))
        if related_task_id is not None:
            args['related_task_id'] = related_task_id
        return self.client.call('link_task', args)

    def reconcile(self, request_id, task_id):
        return self.client.call('reconcile_task', self._lease(dict(request_id=request_id, task_id=task_id)))
