"""Muse 多步网页流程和真实本地取消边界。"""
import json,time
from macos_acceptance import ROOT,c,repo,submit,wait
p=repo('Muse 多步流程')
t=submit('muse-workflow','muse','请执行多步网页验收：从 https://docs.python.org/3/library/urllib.parse.html 页面实际查找 urljoin，沿该页相关链接打开 urllib.request 文档，再沿其链接找到 HTTPError 定义。报告 urljoin("https://example.com/a/b", "../c") 的结果、HTTPError 的基类和 urllib.request.urlopen 默认 timeout 的含义。每项给官方来源的具体链接，并列出实际完成的浏览步骤。只读公开网页，不写文件、不登录、不联系任何人。',p,240,'delegate')
r=wait(t,280)
summary=dict(workflow=dict(task_id=t['id'],result=r,passed=r['state']=='succeeded' and len(r.get('source_urls',[]))>=2))
(ROOT/'muse-workflow.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print('workflow',summary['workflow']['passed'],flush=True)
t=submit('muse-local-cancel','muse','只读浏览 https://docs.python.org/3/library/index.html ，逐页查阅 urllib.parse、urllib.request、urllib.error、http.client、http.server 的官方文档，整理详细对照表；不写文件、不登录、不联系他人。',p,180,'delegate')
accepted=False;end=time.monotonic()+100
while time.monotonic()<end:
    ev=c.call('get_events',{'task_id':t['id'],'limit':200})['events']
    if any(e['kind']=='accepted' for e in ev):accepted=True;break
    if c.call('get_result',{'task_id':t['id']})['ready']:break
    time.sleep(.3)
start=time.monotonic();cancel=c.call('cancel_task',{'task_id':t['id']});r=wait(t,40)
summary['local_cancel']=dict(task_id=t['id'],accepted=accepted,cancel=cancel,result=r,elapsed=round(time.monotonic()-start,2),passed=accepted and r['state']=='cancelled' and r.get('remote_cancelled') is False and cancel['cancel_scope']=='local_wait_only')
(ROOT/'muse-workflow.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print('local_cancel',summary['local_cancel']['passed'],flush=True)
