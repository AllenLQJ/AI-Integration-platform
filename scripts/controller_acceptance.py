"""Real Codex controller calling the Hub MCP, with executor recursion isolated."""
import json
import sys
import threading
import time
from pathlib import Path
from agent_hub.config import data_dir, adapters
from agent_hub.processes import environment, run_process

root = data_dir()
repo = next((root / 'acceptance').iterdir())
exe = adapters(root)['codex'].executable
prompt = ('Use ONLY the agent_hub MCP tools. Call list_agents. Then submit_task with '
          'request_id="controller-accept-20261009-1", agent="claude", mode="read_only", '
          'timeout_seconds=120, cwd=' + json.dumps(str(repo)) + ', '
          'prompt="Reply exactly CONTROL_HUB2026. Do not use tools.". '
          'Record the returned id. Query get_task and get_result until ready=true. '
          'Do not submit duplicates with different request ids. Return the task id and final answer. '
          'Do not use shell tools or access any other MCP server.')
argv = [exe, 'exec', '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check', '--json',
        '--sandbox', 'read-only', '-c', 'approval_policy="never"', '-c', 'features.apps=false',
        '-c', 'windows.sandbox="mxc"',
        '-c', 'mcp_servers.agent_hub.command=' + json.dumps(sys.executable),
        '-c', 'mcp_servers.agent_hub.args=["-m","agent_hub","mcp"]',
        '-c', 'mcp_servers.agent_hub.required=true',
        '-c', 'mcp_servers.agent_hub.startup_timeout_sec=60',
        '-c', 'mcp_servers.agent_hub.env.PYTHONUTF8="1"', prompt]
# This dedicated probe trusts only the explicitly authorized Hub tools for this process.
# It does not change the user's global approval policy or MCP tool approvals.
argv[-1:-1] = sum((['-c', 'mcp_servers.agent_hub.tools.'+name+'.approval_mode="approve"',
                   '-c', 'mcp_servers.agent_hub.tools.'+name+'.enabled=true']
                  for name in ('list_agents','submit_task','get_task','get_events','get_result')), [])
rc, stdout, stderr, logs = run_process(argv, repo, environment(adapters(root)['codex'].env), threading.Event(),
                                      time.time()+180, root / 'controller-acceptance', lambda *a: None)
events = []
for line in stdout.splitlines():
    try: events.append(json.loads(line))
    except ValueError: pass
calls = [e.get('item', {}) for e in events if e.get('item', {}).get('type') == 'mcp_tool_call']
answers = [e['item']['text'] for e in events if e.get('item', {}).get('type') == 'agent_message']
result = {'exit_code': rc, 'calls': calls, 'answer': answers, 'logs': logs}
(root / 'controller-acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'exit_code':rc,'tools':[c.get('tool') for c in calls], 'answer':answers},ensure_ascii=False),flush=True)
if not calls: raise SystemExit('No actual MCP calls: acceptance did not pass')
