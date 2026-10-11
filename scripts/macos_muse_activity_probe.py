"""只读查询验收会话关联活动，寻找可独立核对的工具/浏览器证据。"""
import asyncio,json
from pathlib import Path
from macos_resilience_common import BASE,ROOT,save,write_json
from agent_hub.adapters.muse import Muse
from agent_hub.processes import environment
async def main():
 from mcp import ClientSession,StdioServerParameters
 from mcp.client.stdio import stdio_client
 d=json.loads((ROOT/'muse-resilience-summary.json').read_text());sid=d['results']['connection-cut']['result']['session_id']
 a=BASE['agents']['muse'];params=StdioServerParameters(command=a['executable'],args=a.get('args',['-m','muse_mcp']),env=environment(dict(a.get('env',{}),MUSE_MCP_ENABLE_RAW='1')))
 async with stdio_client(params) as (reader,writer):
  async with ClientSession(reader,writer) as session:
   await session.initialize()
   response=await session.call_tool('muse_raw',dict(method='activity.list',query_params={'session_id':sid,'limit':20}))
   payload=Muse.response_payload(response);write_json(ROOT/'muse-activity-response.json',payload)
   print('top_keys',list(payload) if isinstance(payload,dict) else type(payload).__name__)
   result=payload.get('result') if isinstance(payload,dict) else None
   print('result_shape',list(result) if isinstance(result,dict) else type(result).__name__)
   if isinstance(result,list):print('count',len(result),'entry_keys',[list(x) if isinstance(x,dict) else type(x).__name__ for x in result[:2]])
asyncio.run(main())
