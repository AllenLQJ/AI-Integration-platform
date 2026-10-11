"""验证当前版本拒绝跨机器 Hub 地址，而非声称跨机器成功。"""
import json,tempfile
from pathlib import Path
from agent_hub.client import Client
from agent_hub.models import HubError
from macos_acceptance import ROOT
checks=[]
for address in ('http://192.168.1.2:8765','http://example.com:8765','https://127.0.0.1:8765'):
 with tempfile.TemporaryDirectory() as td:
  p=Path(td);(p/'connection.json').write_text(json.dumps({'url':address,'token':'diagnostic-placeholder'}))
  try:Client(p).request('/health');code='NOT_REJECTED'
  except HubError as error:code=error.code
  checks.append(dict(address=address,code=code))
summary=dict(checks=checks,passed=all(x['code']=='INVALID_CONNECTION' for x in checks),capability='不支持跨机器 Hub 调度；本轮验证地址边界，未实现新功能')
(ROOT/'boundaries.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps(summary,ensure_ascii=False))
