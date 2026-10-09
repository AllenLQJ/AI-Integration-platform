import argparse
import json
import os


def main():
    parser = argparse.ArgumentParser(description='Local Agent Hub')
    parser.add_argument('command', choices=['mcp', 'daemon', 'status', 'stop'])
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    if args.data_dir: os.environ['AGENT_HUB_DATA_DIR'] = args.data_dir
    from .config import data_dir
    if args.command == 'daemon':
        import sys
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
        print(json.dumps(client.stop() if args.command == 'stop' else client.call('list_agents'),
                         ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
