"""用明确无效凭据验证真实供应商 HTTP 拒绝，不读取真实 token。"""
import json,urllib.request,urllib.error,time
from pathlib import Path
from macos_resilience_common import ROOT,save
s=json.loads((Path.home()/'.claude/settings.json').read_text())['env']
url=s['ANTHROPIC_BASE_URL'].rstrip('/')+'/v1/messages'
body=json.dumps(dict(model=s['ANTHROPIC_MODEL'].replace('[1m]',''),max_tokens=8,messages=[dict(role='user',content='Reply OK')])).encode()
request=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json','anthropic-version':'2023-06-01','Authorization':'Bearer invalid-resilience-probe-not-a-key'})
start=time.monotonic()
try:
 with urllib.request.urlopen(request,timeout=25) as response:
  status=response.status;payload=response.read().decode()
except urllib.error.HTTPError as error:
 status=error.code;payload=error.read().decode()
except Exception as error:
 status=None;payload=type(error).__name__
try:parsed=json.loads(payload)
except ValueError:parsed=payload[:300]
save('invalid-auth-http',dict(passed=status==401,http_status=status,response=parsed,elapsed_seconds=round(time.monotonic()-start,2),source='真实配置的 DeepSeek Anthropic 兼容 HTTP 入口；凭据为固定无效占位符，未调用正常认证。',limitation='HTTP 拒绝证据不能替代 Hub/Claude 的认证错误分类；前次 CLI 无效认证任务只有 timed_out。'))
