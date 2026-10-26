"""Scoped real Muse MCP feasibility probe; no credential output."""
import asyncio,json,os,sys,uuid,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).parent/'results'/'muse.json'
async def main():
    env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','USERPROFILE','HOME','APPDATA','LOCALAPPDATA','TEMP','TMP','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'}}
    env['MUSE_MCP_ENABLE_RAW']='1'
    params=StdioServerParameters(command=os.environ.get('MUSE_PYTHON') or sys.executable,args=['-m','muse_mcp'],env=env)
    result={}
    async with stdio_client(params) as (r,w):
      async with ClientSession(r,w) as s:
        await s.initialize()
        async def call(name,args):
          r=await s.call_tool(name,args,read_timeout_seconds=__import__('datetime').timedelta(seconds=90))
          return (r.structuredContent or {}).get('result')
        previous=json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
        created=previous.get('session_start') or await call('muse_raw',{'method':'session.start','body':{'method':'/api/session/start','params':{'origin':'fresh','lifecycle':'persistent','title':'Agent Hub feasibility probe'}}})
        result['session_start']=created
        print(json.dumps({'session_start':created},ensure_ascii=False),flush=True)
        payload=created.get('result',{}) if isinstance(created,dict) else {}
        if isinstance(payload,dict) and isinstance(payload.get('result'),dict): payload=payload['result']
        sid=payload.get('session_id') if isinstance(payload,dict) else None
        if not sid and isinstance(created,dict):
          sid=(payload.get('session') or {}).get('session_id') if isinstance(payload,dict) else None
        if sid:
          marker='MUSE_HUB_'+uuid.uuid4().hex[:10]
          before=await call('muse_history',{'thread':sid,'limit':3})
          baseline=max((x.get('seq',0) for x in before),default=0) if isinstance(before,list) else 0
          sent=await call('muse_send',{'thread':sid,'message':f'这是连接验证，请只回复 {marker}，不要使用工具或创建任务。','wait_seconds':45})
          reply=sent.get('reply',{}) if isinstance(sent,dict) else {}
          result.update(session_id=sid,baseline=baseline,sent_ok=sent.get('ok') if isinstance(sent,dict) else False,marker=marker,reply_seq=reply.get('seq'),reply_text=reply.get('text'),reply_to_message_id=reply.get('reply_to_message_id'),marker_pass=reply.get('text','').strip()==marker)
          history=await call('muse_history',{'thread':sid,'limit':6})
          result['events']=[{k:x.get(k) for k in ('seq','role','message_id','reply_to_message_id','text')} for x in history if x.get('seq',0)>baseline] if isinstance(history,list) else history
        OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__ == '__main__':
    asyncio.run(main())
