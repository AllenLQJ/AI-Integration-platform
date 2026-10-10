"""Disposable feasibility probes, not Agent Hub implementation."""
import json, os, subprocess, sys, tempfile, time, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs' / 'probes' / 'results'
OUT.mkdir(exist_ok=True)
ENV = {k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY','TOKEN','SECRET','PASSWORD'))}
ENV.update(HTTP_PROXY='http://127.0.0.1:7897', HTTPS_PROXY='http://127.0.0.1:7897', ALL_PROXY='http://127.0.0.1:7897')
ENV.pop('CLAUDECODE',None)

def run(argv, cwd, timeout=150):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        p = subprocess.Popen(argv, cwd=cwd, env=ENV, stdout=out, stderr=err)
        try: p.wait(timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],capture_output=True)
            p.wait(10)
            return {'exit':p.returncode,'timeout':True}
        out.seek(0); err.seek(0)
        text = out.read().decode('utf-8',errors='replace')
        events=[]
        for line in text.splitlines():
            try: events.append(json.loads(line))
            except ValueError: pass
        # Keep full CLI output out of logs: only selected structured fields.
        errtext=err.read().decode('utf-8',errors='replace')
        import re
        safe=re.sub(r'sk-[A-Za-z0-9_-]+','[REDACTED]',errtext)
        return {'exit':p.returncode,'events':events,'stderr_length':len(errtext),'error_summary':safe[:1500] if p.returncode else None}

def main(agent):
    token='HUB_'+uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='hub 探针 space ') as td:
        if agent=='codex':
            exe=r'C:\Users\95439\AppData\Local\OpenAI\Codex\bin\979a96ce184041d1\codex.exe'
            base=[exe,'exec','--ignore-user-config','--ignore-rules','--skip-git-repo-check','--json']
            first=run(base+['--sandbox','read-only',f'Remember marker {token}. Reply exactly {token}. Do not use tools.'],td)
            sid=next((e.get('thread_id') for e in first.get('events',[]) if e.get('type')=='thread.started'),None)
            texts=[e.get('item',{}).get('text') for e in first.get('events',[]) if e.get('item',{}).get('type')=='agent_message']
            follow=run([exe,'exec','resume','--ignore-user-config','--ignore-rules','--skip-git-repo-check','--json',sid,'Reply only the marker I asked you to remember. Do not use tools.'],td) if sid and first['exit']==0 else {}
            replies=[e.get('item',{}).get('text') for e in follow.get('events',[]) if e.get('item',{}).get('type')=='agent_message']
        else:
            entry=Path(os.environ['APPDATA'])/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
            config=Path(td)/'mcp-empty.json'
            config.write_text('{"mcpServers":{}}',encoding='utf-8')
            base=[str(entry),'-p','--output-format','json','--tools=','--strict-mcp-config',f'--mcp-config={config}']
            first=run(base+['--',f'Remember marker {token}. Reply exactly {token}. Do not use tools.'],td)
            e=first.get('events',[{}])[-1] if first.get('events') else {}
            sid=e.get('session_id');texts=[e.get('result')]
            follow=run(base+['--resume',sid,'--','Reply only the marker I asked you to remember. Do not use tools.'],td) if sid and first['exit']==0 else {}
            e2=follow.get('events',[{}])[-1] if follow.get('events') else {}
            replies=[e2.get('result')]
        result={'agent':agent,'marker':token,'cwd_contains_unicode_and_spaces':True,'first_exit':first.get('exit'),'first_timeout':first.get('timeout',False),'session_id':sid,'first_reply':texts,'resume_exit':follow.get('exit'),'resume_timeout':follow.get('timeout',False),'resume_reply':replies,'resume_pass':token in replies,'error_summary':first.get('error_summary'),'errors':[{'type':e.get('type'),'message':e.get('message')} for e in first.get('events',[]) if e.get('type')=='error']}
        (OUT/f'{agent}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__': main(sys.argv[1])
