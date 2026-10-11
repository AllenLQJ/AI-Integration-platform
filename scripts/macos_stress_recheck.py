"""独立空闲后台的六任务并发复验，排除其他验收任务占用额度。"""
import runpy
import macos_acceptance as m
from agent_hub.client import Client
from agent_hub.config import private_dir,data_dir
m.ROOT=data_dir()/'mac-stress-recheck-20261011';private_dir(m.ROOT)
(m.ROOT/'config.json').write_text((data_dir()/'config.json').read_text());(m.ROOT/'config.json').chmod(0o600)
m.c=Client(m.ROOT)
try:runpy.run_path('scripts/macos_stress_acceptance.py',run_name='__main__')
finally:m.c.stop()
