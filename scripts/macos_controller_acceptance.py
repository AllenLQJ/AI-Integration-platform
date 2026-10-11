"""Mac 真实 Codex 总控经 MCP 调度真实 Claude；使用独立验收后台。"""
import json,sys,threading,time
from pathlib import Path
from agent_hub.config import adapters,data_dir
from agent_hub.processes import environment,run_process
root=data_dir()/'mac-full-acceptance-20261011'
repo=root/'生命周期 cancel'
exe=adapters(root)['codex'].executable
names=('list_agents','submit_task','get_task','get_events','wait_for_task','get_result')
prompt=('Use ONLY agent_hub MCP tools. Call list_agents then submit exactly one task with request_id="mac-controller-20261011", agent="claude", mode="text_only", timeout_seconds=120, cwd='+json.dumps(str(repo))+', prompt="Reply exactly MAC_CONTROLLER_PASS. Do not use tools.". Call get_task and get_events. Call wait_for_task repeatedly with timeout_seconds=10 and returned next_seq until terminal. Then get_result and return the task id and exact answer. Do not use shell tools.')
argv=[exe,'exec','--ignore-user-config','--ignore-rules','--skip-git-repo-check','--json','--sandbox','read-only','-c','approval_policy="never"','-c','features.apps=false','-c','mcp_servers.agent_hub.command='+json.dumps(sys.executable),'-c','mcp_servers.agent_hub.args=["-m","agent_hub","mcp"]','-c','mcp_servers.agent_hub.env.AGENT_HUB_DATA_DIR='+json.dumps(str(root)),'-c','mcp_servers.agent_hub.required=true','-c','mcp_servers.agent_hub.startup_timeout_sec=60']
for name in names: argv+=['-c','mcp_servers.agent_hub.tools.'+name+'.approval_mode="approve"','-c','mcp_servers.agent_hub.tools.'+name+'.enabled=true']
argv+=[prompt]
rc,out,err,logs=run_process(argv,repo,environment(adapters(root)['codex'].env),threading.Event(),time.time()+240,root/'controller-logs',lambda *a:None)
events=[]
for line in out.splitlines():
    try:events.append(json.loads(line))
    except ValueError:pass
calls=[e['item'] for e in events if e.get('item',{}).get('type')=='mcp_tool_call']
answers=[e['item']['text'] for e in events if e.get('item',{}).get('type')=='agent_message']
tools=[c.get('tool') for c in calls]
result=dict(exit_code=rc,calls=calls,answer=answers,logs=logs,passed=rc==0 and set(names)<=set(tools) and any('MAC_CONTROLLER_PASS' in a for a in answers))
(root/'controller.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(dict(exit_code=rc,tools=tools,answer=answers,passed=result['passed']),ensure_ascii=False),flush=True)
