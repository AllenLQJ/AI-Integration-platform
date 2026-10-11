"""缩短仅探针 CLI 的重试，核实真实 401 与 Hub 失败终态。"""
import copy,json
from pathlib import Path
from macos_resilience_common import BASE,ROOT,make_client,repo,save,submit,wait,write_json
settings=json.loads((Path.home()/'.claude/settings.json').read_text())
directory=ROOT/'claude-isolated/invalid-auth-recheck'
settings['env']['ANTHROPIC_AUTH_TOKEN']='invalid-resilience-probe-not-a-key'
settings['env'].update(CLAUDE_CODE_MAX_RETRIES='1',API_TIMEOUT_MS='15000')
write_json(directory/'settings.json',settings)
conf=copy.deepcopy(BASE);conf['agents']['claude'].setdefault('env',{}).update(CLAUDE_CONFIG_DIR=str(directory),CLAUDE_CODE_MAX_RETRIES='1',API_TIMEOUT_MS='15000')
# 调试只用于无效凭据的隔离配置，日志不出私有目录。
wrapper=directory/'claude-probe';actual=conf['agents']['claude']['executable']
wrapper.write_text('#!'+str(Path.cwd()/'.venv/bin/python')+'\nimport os,sys\nos.execv('+repr(actual)+', ['+repr(actual)+', "--debug-file", '+repr(str(directory/'debug.log'))+'] + sys.argv[1:])\n');wrapper.chmod(0o700)
conf['agents']['claude']['executable']=str(wrapper)
c,root=make_client('claude-invalid-auth-recheck',conf)
try:
 t=submit(c,'invalid-auth-recheck','claude','Reply exactly SHOULD_NOT_SUCCEED.',repo('认证复验'),60)
 r=wait(c,t,80)
 debug=(directory/'debug.log').read_text() if (directory/'debug.log').exists() else ''
 logs=r.get('artifacts',{}).get('logs',{});output='\n'.join(Path(v).read_text() for v in logs.values())
 save('claude-invalid-auth-recheck',dict(task_id=t['id'],result=r,observed_401='401' in debug or '401' in output,debug_path=str(directory/'debug.log'),passed=r['state']=='failed' and ('401' in debug or '401' in output),limitation='只减少隔离探针的 CLI 重试，不更改用户配置；无效凭据不等于自然过期。'))
finally:c.stop()
