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
    def request(self, path, body=None):
        connection = json.loads((self.root / 'connection.json').read_text(encoding='utf-8'))
        url = connection['url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise HubError('INVALID_CONNECTION', '服务地址必须为回环地址')
        request = urllib.request.Request(url + path,
                         data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
                         headers={'Authorization': 'Bearer ' + connection['token'], 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=15) as response: return json.load(response)
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
        raise HubError('DAEMON_UNAVAILABLE', '后台服务未启动，请检查私有目录中的 daemon.log')
    def call(self, tool, arguments=None):
        self.ensure()
        # Mutating calls are never automatically retried after uncertain transport errors.
        response = self.request('/rpc', {'tool': tool, 'arguments': arguments or {}})
        if 'error' in response:
            raise HubError(response['error']['code'], response['error']['message'])
        return response['result']
    def stop(self):
        return self.request('/shutdown', {})
