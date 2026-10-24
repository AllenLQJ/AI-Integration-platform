import argparse
import json
import os
import sys

TOOLS = ('list_nodes', 'transfer_artifact', 'list_agents', 'submit_task', 'get_task', 'get_events', 'wait_for_task',
         'get_result', 'continue_task', 'resume_task', 'cancel_task',
         'ide_next_task', 'ide_task_update', 'ide_task_status',
         'verify_task', 'get_work_item', 'record_verification',
         'claim_controller', 'switch_executor', 'record_external_verification')


def open_dashboard(client):
    import webbrowser
    from urllib.error import HTTPError
    from urllib.parse import urlsplit
    from .models import HubError
    try:
        client.ensure()
        result = client.request('/dashboard/ticket', {})
    except HTTPError as error:
        code = 'DASHBOARD_ASSETS_MISSING' if error.code == 503 else 'DASHBOARD_UNAVAILABLE'
        error.close()
        raise HubError(code, '看板不可用，请检查安装包静态资源及后台服务版本') from None
    except (OSError, ValueError):
        raise HubError('DASHBOARD_UNAVAILABLE', '无法连接本地看板服务') from None
    try:
        url = result['url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.path != '/dashboard/'
                or parsed.query or not parsed.fragment.startswith('ticket=')):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HubError('DASHBOARD_UNAVAILABLE', '看板启动响应无效') from None
    try:
        opened = webbrowser.open(url, new=2)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        raise HubError('BROWSER_UNAVAILABLE', '无法打开默认浏览器，请配置浏览器后重新运行 agent-hub dashboard')
    return {'opened': True}


def main():
    parser = argparse.ArgumentParser(description='Local Agent Hub')
    parser.add_argument('command', choices=['mcp', 'daemon', 'status', 'stop', 'rpc', 'show', 'plugins', 'check-config', 'dashboard'])
    parser.add_argument('tool', nargs='?')
    parser.add_argument('--arguments', default='{}', help='rpc 参数 JSON 对象；用 - 从 stdin 读取')
    parser.add_argument('--data-dir')
    parser.add_argument('--ssh-host', help='已有 SSH 主机别名；调用目标机 Hub')
    parser.add_argument('--remote-python', help='目标机虚拟环境 Python 绝对路径')
    parser.add_argument('--remote-data-dir', help='目标机 Hub 私有数据目录绝对路径')
    parser.add_argument('--remote-platform', choices=['posix', 'darwin', 'linux', 'windows'])
    parser.add_argument('--remote-shell', choices=['posix', 'cmd', 'powershell'])
    args = parser.parse_args()
    if args.command == 'rpc' and not args.tool:
        parser.error('rpc 需要工具名称')
    if args.command == 'rpc' and args.tool not in TOOLS:
        parser.error('未知 RPC 工具')
    if args.command == 'show' and not args.tool:
        parser.error('show 需要 task_id')
    if args.command not in {'rpc', 'show'} and args.tool:
        parser.error('只有 rpc 接受工具名称')
    if args.ssh_host and (args.command not in {'mcp', 'status', 'rpc', 'show'} or not args.remote_python or not args.remote_data_dir):
        parser.error('SSH 仅支持 mcp/status/rpc/show，需 remote-python 和 remote-data-dir')
    if not args.ssh_host and (args.remote_python or args.remote_data_dir or args.remote_platform or args.remote_shell):
        parser.error('远端路径需要 ssh-host')
    try:
        arguments = json.loads(sys.stdin.read() if args.arguments == '-' else args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError()
    except ValueError:
        parser.error('--arguments 需要 JSON 对象')
    if args.data_dir: os.environ['AGENT_HUB_DATA_DIR'] = args.data_dir
    from .config import data_dir
    if args.command in {'plugins', 'check-config'}:
        from .models import HubError
        from .plugins import installed_plugins
        from .config import adapters, scheduler_options, verification_options, artifact_transfer_options
        try:
            if args.command == 'plugins':
                result = {'plugins': installed_plugins()}
            else:
                root = data_dir()
                scheduler_options(root); verification_options(root); artifact_transfer_options(root)
                result = {'agents': {name: adapter.diagnose() for name, adapter in adapters(root).items()}}
        except HubError as error:
            print(json.dumps({'error': error.payload()}, ensure_ascii=False))
            raise SystemExit(1) from None
        except (ValueError, TypeError, KeyError, OSError) as error:
            print(json.dumps({'error': {'code': 'INVALID_CONFIG', 'message': type(error).__name__}}, ensure_ascii=False))
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == 'daemon':
        root = data_dir()
        from .config import private_dir
        private_dir(root)
        log = (root / 'daemon.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = log
        from .daemon import run
        run(root)
    elif args.command == 'mcp':
        from .mcp_server import run
        if args.ssh_host:
            from .ssh_client import SshClient
            from .models import HubError, redact
            try:
                connection = SshClient(args.ssh_host, args.remote_python, args.remote_data_dir,
                                       platform=args.remote_platform or 'posix', shell=args.remote_shell)
            except HubError as error:
                # stdout belongs to the MCP framing protocol, not CLI JSON.
                print(json.dumps({'error': {'code': error.code, 'message': redact(str(error))}},
                                 ensure_ascii=False), file=sys.stderr)
                raise SystemExit(1) from None
            run(connection)
        else:
            run()
    else:
        from .client import Client
        from .models import HubError, redact
        try:
            if args.ssh_host:
                from .ssh_client import SshClient
                client = SshClient(args.ssh_host, args.remote_python, args.remote_data_dir,
                                   platform=args.remote_platform or 'posix', shell=args.remote_shell)
            else:
                client = Client()
            result = (open_dashboard(client) if args.command == 'dashboard' else
                      client.stop() if args.command == 'stop' else
                      client.call(args.tool, arguments) if args.command == 'rpc' else
                      client.call('get_work_item', {'task_id': args.tool}) if args.command == 'show' else client.call('list_agents'))
        except HubError as error:
            print(json.dumps({'error': {'code': error.code, 'message': redact(str(error))}}, ensure_ascii=False))
            raise SystemExit(1)
        if args.command == 'show':
            from .verification import render
            print(render(result))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
