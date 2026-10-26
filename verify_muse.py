"""Verify the installed Muse MCP server without displaying credentials."""
import asyncio
import argparse
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(send=False, python=None):
    params = StdioServerParameters(
        command=python or os.environ.get('MUSE_PYTHON') or sys.executable,
        args=['-m', 'muse_mcp'],
        env={name: value for name, value in os.environ.items() if name.upper() in {
            'PATH', 'SYSTEMROOT', 'USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA',
            'TEMP', 'TMP', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
        }},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            status = await session.call_tool('muse_status', {})
            state = (status.structuredContent or {}).get('result', {})
            print(json.dumps({
                'server': init.serverInfo.model_dump(),
                'tools': [tool.name for tool in tools.tools],
                'status': {key: state.get(key) for key in ('ok', 'authenticated', 'session_count', 'error')},
            }, ensure_ascii=False, indent=2))
            if not state.get('ok'):
                raise SystemExit(1)
            if send and state.get('ok'):
                reply = await session.call_tool('muse_send', {
                    'message': 'This is a connection test. Reply only: MUSE_CONNECTION_OK. Do not use tools or perform any other actions.',
                    'wait_seconds': 45,
                })
                print(json.dumps({'send_result': reply.structuredContent}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', help='Python with muse_mcp installed (default: MUSE_PYTHON or current Python)')
    parser.add_argument('--send', action='store_true', help='Send one connection-test message after successful status check')
    options = parser.parse_args()
    asyncio.run(main(options.send, options.python))
