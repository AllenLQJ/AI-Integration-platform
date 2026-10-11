"""本机独立网页的两步交互挑战；随机结果不放入委托提示。"""
import html,json,secrets,threading,time,urllib.parse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from macos_resilience_common import make_client,repo,save,submit,wait
step=secrets.token_hex(10);challenge=str(secrets.randbelow(900000)+100000);marker='BROWSER_'+secrets.token_hex(12)
requests=[]
class Page(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,body,status=200):
  data=body.encode();self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  requests.append(dict(method='GET',path=self.path,time=time.time()))
  if self.path=='/start':self.reply('<!doctype html><html lang="zh"><meta charset="utf-8"><title>验收步骤一</title><h1>本机网页交互验收</h1><a href="/step/'+step+'">下一步</a></html>')
  elif self.path=='/step/'+step:self.reply('<!doctype html><html lang="zh"><meta charset="utf-8"><title>验收步骤二</title><p>校验数字：'+challenge+'</p><form action="/complete" method="post"><label>输入上方数字<input name="code"></label><input type="hidden" name="step" value="'+step+'"><button type="submit">提交并显示结果</button></form></html>')
  else:self.reply('Not found',404)
 def do_POST(self):
  data=self.rfile.read(int(self.headers.get('Content-Length',0)));fields=urllib.parse.parse_qs(data.decode())
  correct=self.path=='/complete' and fields.get('step')==[step] and fields.get('code')==[challenge]
  requests.append(dict(method='POST',path=self.path,time=time.time(),valid=correct))
  self.reply('<h1>验收标记</h1><p>'+marker+'</p>' if correct else '校验失败',200 if correct else 400)
server=ThreadingHTTPServer(('127.0.0.1',0),Page);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
c,root=make_client('muse-browser-challenge');p=repo('浏览器交互挑战')
try:
 prompt=('这是已授权的本机测试页面，使用已配对 Mac 的浏览器操作 http://127.0.0.1:'+str(server.server_port)+'/start 。点击“下一步”，读取校验数字，输入表单并点击“提交并显示结果”，最后报告页面显示的验收标记。只操作这个测试页面，不用终端、HTTP 脚本或本机文件工具，不写其他文件、不登录、不联系任何人。如果你的浏览器不能访问已配对 Mac 的 localhost，请如实报告，不能猜测结果。')
 t=submit(c,'browser-challenge','muse',prompt,p,150,'delegate');r=wait(c,t,175)
 steps=any(x['path']=='/start' for x in requests) and any(x['path']=='/step/'+step for x in requests) and any(x.get('valid') for x in requests)
 save('muse-browser-challenge',dict(task_id=t['id'],result=r,requests=requests,marker=marker,server_steps_completed=steps,passed=r['state']=='succeeded' and marker in r.get('answer','') and steps,limitation='服务器请求可证明交互路径及随机结果，但缺乏浏览器动作日志时，不能独立排除执行器改用 HTTP 工具。'))
finally:
 c.stop();server.shutdown();server.server_close();thread.join(2)
