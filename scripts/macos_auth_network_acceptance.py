"""真实执行器的隔离认证故障、局部断连与后续恢复验收。"""
import copy,json,socket,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from macos_resilience_common import BASE,ROOT,checksum,make_client,repo,save,submit,wait,write_json

original=Path.home()/'.claude/settings.json'
original_hash=checksum(original)
settings=json.loads(original.read_text())
p=repo('认证与网络')
results={}
class Reject(BaseHTTPRequestHandler):
    calls=0
    def log_message(self,*args):pass
    def do_POST(self):
        type(self).calls+=1
        # 不读取或记录鉴权字段；这里只模拟不可用 HTTP 服务。
        size=int(self.headers.get('Content-Length',0));self.rfile.read(size)
        body=b'{"type":"error","error":{"type":"overloaded_error","message":"isolated resilience diagnostic"}}'
        self.send_response(503);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    do_GET=do_POST
server=ThreadingHTTPServer(('127.0.0.1',0),Reject)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
for kind in ('invalid-auth','unavailable-endpoint'):
    conf=copy.deepcopy(BASE);directory=ROOT/'claude-isolated'/kind
    cloned=copy.deepcopy(settings)
    if kind=='invalid-auth':cloned['env']['ANTHROPIC_AUTH_TOKEN']='invalid-resilience-probe-not-a-key'
    else:
        cloned['env']['ANTHROPIC_AUTH_TOKEN']='local-diagnostic-placeholder'
        cloned['env']['ANTHROPIC_BASE_URL']='http://127.0.0.1:'+str(server.server_port)
    write_json(directory/'settings.json',cloned)
    conf['agents']['claude'].setdefault('env',{})['CLAUDE_CONFIG_DIR']=str(directory)
    c,root=make_client('claude-'+kind,conf)
    try:
        t=submit(c,kind,'claude','Reply exactly SHOULD_NOT_SUCCEED.',p,35)
        r=wait(c,t,55)
        # 真实错误日志存私有目录，只输出终态，不输出认证配置。
        results[kind]=dict(task_id=t['id'],result=r,local_http_requests=Reject.calls,
                          passed=r['state'] in {'failed','timed_out'} and (kind!='unavailable-endpoint' or Reject.calls>0))
        save('claude-'+kind,results[kind])
    finally:c.stop()
server.shutdown();server.server_close();thread.join(2)
c,root=make_client('healthy-after-fault')
try:
    for agent,mode in [('claude','text_only'),('codex','read_only')]:
        t=submit(c,'healthy-'+agent,agent,'Reply exactly RESILIENCE_RECOVERED. Do not use tools.',p,90,mode)
        r=wait(c,t,115);results['healthy-'+agent]=dict(task_id=t['id'],result=r,passed=r['state']=='succeeded' and 'RESILIENCE_RECOVERED' in r.get('answer',''))
finally:c.stop()
results['original_settings_unchanged']=checksum(original)==original_hash
save('auth-network-summary',dict(cases={k:v.get('passed') for k,v in results.items() if isinstance(v,dict)},results=results,passed=all(v.get('passed') for v in results.values() if isinstance(v,dict)) and results['original_settings_unchanged'],limitation='无效认证探针不等于主动让真实登录过期；503 为局部真实 HTTP 故障，非全局断网。'))
