"""真实 Muse 隔离认证错误、局部 TLS 连接切断及恢复。"""
import asyncio,copy,json,os,threading,time
from pathlib import Path
from macos_fault_proxy import FaultProxy
from macos_resilience_common import BASE,ROOT,checksum,make_client,repo,save,submit,wait,write_json
from agent_hub.adapters.muse import Muse
from agent_hub.processes import environment
p=repo('Muse 异常恢复');results={}
source=Path.home()/'.config/muse-cli/config.json';cookies=Path.home()/'.config/muse-cli/cookies.txt'
original={str(x):checksum(x) for x in (source,cookies)}
config=json.loads(source.read_text())
bad=ROOT/'muse-invalid-auth';bad.mkdir(exist_ok=True);bad.chmod(0o700)
write_json(bad/'config.json',dict(config,cookies_file=str(bad/'cookies.txt')))
(bad/'cookies.txt').write_text('auth_probe=invalid-resilience-cookie');(bad/'cookies.txt').chmod(0o600)
conf=copy.deepcopy(BASE);conf['agents']['muse'].setdefault('env',{})['MUSE_CONFIG_DIR']=str(bad)
c,root=make_client('muse-invalid-auth',conf)
try:
 t=submit(c,'invalid-muse-auth','muse','Reply exactly SHOULD_NOT_SUCCEED.',p,70,'delegate');r=wait(c,t,95)
 ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
 results['invalid-auth']=dict(task_id=t['id'],result=r,events=ev,passed=r['state']=='failed' and r.get('error_code')=='AUTH_REQUIRED' and not any(e['kind']=='accepted' for e in ev))
 save('muse-invalid-auth',results['invalid-auth'])
finally:c.stop()
proxy=FaultProxy();conf=copy.deepcopy(BASE)
conf['agents']['muse'].setdefault('env',{}).update(HTTP_PROXY=proxy.url,HTTPS_PROXY=proxy.url,ALL_PROXY=proxy.url)
c,root=make_client('muse-cut-connection',conf)
try:
 t=submit(c,'muse-cut-after-accept','muse','请只读查阅 https://docs.python.org/3/library/asyncio-task.html 和 https://docs.python.org/3/library/asyncio-sync.html，比较 wait_for、timeout、Event，返回三条简明结论和来源链接。禁止写文件、登录或联系他人。',p,180,'delegate')
 end=time.monotonic()+90;accepted=False
 while time.monotonic()<end:
  ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
  if any(e['kind']=='accepted' for e in ev):accepted=True;break
  if c.call('get_result',{'task_id':t['id']})['ready']:break
  time.sleep(.05)
 before=proxy.connects
 if accepted and before>0:
  proxy.drop();time.sleep(2);proxy.restore()
 r=wait(c,t,200);ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
 task=c.call('get_task',{'task_id':t['id']})
 results['connection-cut']=dict(task_id=t['id'],result=r,events=ev,proxy_connects_before=before,proxy_connects_after=proxy.connects,cut_performed=accepted and before>0,
 passed=accepted and before>0 and r['state'] in {'succeeded','interrupted'} and sum(e['kind']=='accepted' for e in ev)==1,
 limitation='succeeded 表示读取恢复；interrupted 表示安全保留远端未知，不能当作本任务成功。')
 save('muse-connection-cut',results['connection-cut'])
 async def history():
  from mcp import ClientSession,StdioServerParameters
  from mcp.client.stdio import stdio_client
  a=BASE['agents']['muse'];params=StdioServerParameters(command=a['executable'],args=a.get('args',['-m','muse_mcp']),env=environment(a.get('env',{})))
  async with stdio_client(params) as (reader,writer):
   async with ClientSession(reader,writer) as session:
    await session.initialize();response=await session.call_tool('muse_history',dict(thread=task['session_id'],limit=100))
    return Muse.response_payload(response)
 if task.get('session_id'):
  h=asyncio.run(history());write_json(ROOT/'muse-cut-scoped-history.json',h)
  messages=[e for e in h if isinstance(e,dict) and e.get('role') in {'user','message.user'} and '[HUB:'+t['id']+']' in e.get('text','')]
  results['connection-cut']['matching_user_messages']=len(messages)
  results['connection-cut']['passed'] &= len(messages)==1
finally:
 c.stop();proxy.close()
c,root=make_client('muse-healthy-after-fault')
try:
 t=submit(c,'muse-recovered','muse','请只回复 MUSE_RESILIENCE_RECOVERED，不使用工具。',p,120,'delegate');r=wait(c,t,145)
 results['healthy-after-fault']=dict(task_id=t['id'],result=r,passed=r['state']=='succeeded' and 'MUSE_RESILIENCE_RECOVERED' in r.get('answer',''))
finally:c.stop()
unchanged=all(checksum(path)==digest for path,digest in original.items())
save('muse-resilience-summary',dict(results=results,cases={k:v['passed'] for k,v in results.items()},original_auth_files_unchanged=unchanged,passed=unchanged and all(v['passed'] for v in results.values()),limitation='无效 Cookie 为隔离副本；不等于主动撤销真实登录。TLS 连接切断仅作用于专用代理进程。'))
