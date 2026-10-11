import argparse
import json
import os
import sys

TOOLS = ('list_agents', 'submit_task', 'get_task', 'get_events', 'wait_for_task',
         'get_result', 'continue_task', 'cancel_task')


def main():
    parser = argparse.ArgumentParser(description='Local Agent Hub')
    parser.add_argument('command', choices=['mcp', 'daemon', 'status', 'stop', 'rpc'])
    parser.add_argument('tool', nargs='?', choices=TOOLS)
    parser.add_argument('--arguments', default='{}', help='rpc 参数 JSON 对象；用 - 从 stdin 读取')
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    if args.command == 'rpc' and not args.tool:
        parser.error('rpc 需要工具名称')
    if args.command != 'rpc' and args.tool:
        parser.error('只有 rpc 接受工具名称')
    try:
        arguments = json.loads(sys.stdin.read() if args.arguments == '-' else args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError()
    except ValueError:
        parser.error('--arguments 需要 JSON 对象')
    if args.data_dir: os.environ['AGENT_HUB_DATA_DIR'] = args.data_dir
    from .config import data_dir
    if args.command == 'daemon':
        root = data_dir()
        from .config import private_dir
        private_dir(root)
        log = (root / 'daemon.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = log
        from .daemon import run
        run(root)
    elif args.command == 'mcp':
        from .mcp_server import run
        run()
    else:
        from .client import Client
        client = Client()
        from .models import HubError, redact
        try:
            result = (client.stop() if args.command == 'stop' else
                      client.call(args.tool, arguments) if args.command == 'rpc' else client.call('list_agents'))
        except HubError as error:
            print(json.dumps({'error': {'code': error.code, 'message': redact(str(error))}}, ensure_ascii=False))
            raise SystemExit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
