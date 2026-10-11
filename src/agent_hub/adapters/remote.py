"""本地队列向另一台机器的 Hub 派发，工作区始终由目标机管理。"""
import json
import time
import shutil
from pathlib import Path
from ..models import HubError, TERMINAL
from ..ssh_client import SshClient


class Remote:
    capabilities = ['delegate', 'continue']
    remote_cancel = True
    cancel_scope = 'remote_hub_task'
    cancellation_timeout = 12

    def __init__(self, host, python, data_dir, agent, cwd, mode):
        self.client = SshClient(host, python, data_dir)
        if (not isinstance(agent, str) or not agent or not isinstance(cwd, str) or not cwd.startswith('/')
                or mode not in {'text_only', 'read_only', 'edit_worktree', 'delegate'}):
            raise HubError('INVALID_CONFIG', '远端 agent/cwd/mode 配置不正确')
        self.agent, self.cwd, self.mode = agent, cwd, mode

    def permission_policy(self, mode):
        return {'enforced_by': 'remote_hub_and_executor', 'enforced': False,
                'workspace': 'remote_machine', 'remote_mode': self.mode, 'os_sandbox': False,
                'allows_write': self.mode in {'edit_worktree', 'delegate'},
                'scope_note': '权限与工作区由目标机执行器控制；本机 cwd 不上传，不自动同步文件'}

    def diagnose(self):
        # list_agents 不递归请求另一 Hub，允许两机互相配置而不形成诊断循环。
        return {'available': shutil.which(self.client.executable) is not None,
                'authentication': '已配置 SSH；目标机可达性与执行器认证由实际任务确认',
                'remote_connection_verified': False,
                'capabilities': self.capabilities, 'host': self.client.host,
                'remote_agent': self.agent, 'remote_mode': self.mode,
                'remote_cancel': True, 'cancel_scope': self.cancel_scope,
                'permissions': {'delegate': self.permission_policy('delegate')}}

    def run(self, task, cancel, event):
        path = Path(task['log_dir']) / 'remote-task.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        binding = {'host': self.client.host, 'request_id': 'hub-remote-' + task['id']}
        def save():
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(binding, ensure_ascii=False), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(path)
        def call(tool, args):
            return self.client.call(tool, args, timeout=max(.1, min(10, task['deadline'] - time.time())))
        if cancel.is_set() or time.time() >= task['deadline']:
            raise HubError('CANCELLED', '远端任务尚未提交')
        save()
        args = {'request_id': binding['request_id'], 'prompt': task['prompt'],
                'timeout_seconds': max(10, min(7200, int(task['deadline'] - time.time())))}
        if task.get('resume_session'):
            args['task_id'] = task['resume_session']
            remote = call('continue_task', args)
        else:
            if task.get('context') is not None:
                args['context'] = task['context']
            remote = call('submit_task', dict(args, agent=self.agent, cwd=self.cwd, mode=self.mode))
        rid = remote.get('id') if isinstance(remote, dict) else None
        if not isinstance(rid, str) or not rid:
            raise HubError('DELIVERY_UNKNOWN', '目标机未返回任务 ID；未重发')
        binding['task_id'] = rid
        save()
        # 代理的续接标识是目标 Hub 任务 ID，目标 Hub 再续接真正原生会话。
        event('session', {'session_id': rid})
        event('accepted', {'remote_task_id': rid, 'host': self.client.host})
        try:
            while True:
                if cancel.is_set() or time.time() >= task['deadline']:
                    raise HubError('CANCELLED' if cancel.is_set() else 'TIMED_OUT', '远端任务等待终止')
                result = call('get_result', {'task_id': rid})
                if (not isinstance(result, dict) or type(result.get('ready')) is not bool
                        or result.get('ready') and result.get('state') not in TERMINAL):
                    raise HubError('REMOTE_STATE_UNKNOWN', '目标机结果格式未确认；保留原任务 ID',
                                   {'remote_task': binding, 'remote_cancelled': False})
                if result.get('ready'):
                    state = result.get('state')
                    info = {'task_id': rid, 'host': self.client.host, 'state': state,
                            'native_session_id': result.get('session_id')}
                    if state != 'succeeded':
                        raise HubError({'cancelled': 'CANCELLED', 'timed_out': 'TIMED_OUT',
                                        'delivery_unknown': 'DELIVERY_UNKNOWN', 'interrupted': 'REMOTE_STATE_UNKNOWN'}
                                       .get(state, 'REMOTE_FAILED'), result.get('error', '目标机任务失败'),
                                       {'remote_task': info, 'remote_cancelled': result.get('remote_cancelled', False)})
                    return {'answer': result.get('answer', ''), 'session_id': rid, 'remote_task': info,
                            'remote_artifacts': result.get('artifacts', {}),
                            'remote_permissions': result.get('permissions', {}), 'source_urls': result.get('source_urls', [])}
                cancel.wait(.5)
        except HubError as error:
            if error.code in {'CANCELLED', 'TIMED_OUT'}:
                details = self.cancel_remote(rid)
                event('remote_cancel', details)
                raise HubError(error.code, str(error), dict(error.details, **details)) from None
            # 读断线不重新派发；目标任务 ID 已留存供恢复查询。
            if error.code == 'REMOTE_UNAVAILABLE':
                raise HubError('REMOTE_STATE_UNKNOWN', '已派发，目标机不可达；保留任务 ID，未重发',
                               {'remote_task': binding, 'remote_cancelled': False}) from None
            raise

    def cancel_remote(self, rid):
        end = time.monotonic() + self.cancellation_timeout
        result = {'remote_cancelled': False, 'cancel_scope': self.cancel_scope,
                  'remote_task_id': rid, 'remote_task_terminal': False}
        try:
            def call(tool):
                return self.client.call(tool, {'task_id': rid}, timeout=min(3, max(.1, end-time.monotonic())))
            response = call('cancel_task')
            while time.monotonic() < end:
                state = call('get_result')
                if state.get('ready') and state.get('state') in TERMINAL:
                    result.update(remote_task_terminal=True, remote_state=state['state'])
                    result['remote_cancelled'] = (state.get('remote_cancelled') is True or
                        (response.get('cancel_scope') in {'local_process_tree', 'not_started'}
                         and state['state'] in {'cancelled', 'timed_out'}))
                    if not result['remote_cancelled']:
                        result['warning'] = '目标 Hub 任务已终止，底层远端工作流停止仍未确认'
                    return result
                time.sleep(.2)
        except (HubError, ValueError, TypeError, AttributeError):
            pass
        result['warning'] = '目标机取消未确认；请按原 remote_task_id 核对，未新建任务'
        return result
