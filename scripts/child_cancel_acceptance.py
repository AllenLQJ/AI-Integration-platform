"""Verify a real Codex shell child is gone after Hub cancellation (Windows)."""
import json
import subprocess
import time
from agent_hub.client import Client
from agent_hub.config import data_dir

root = data_dir()
c = Client(root)
repo = next((root / 'acceptance').iterdir())
task = c.call('submit_task', dict(request_id='real-child-cancel-20261010-1', agent='codex',
    cwd=str(repo), mode='read_only', timeout_seconds=150,
    prompt='This is an authorized local cancellation diagnostic. Run exactly one shell command: '
           'powershell.exe -NoProfile -Command "Start-Sleep -Seconds 60". '
           'It only sleeps and changes no files. Wait for it normally; the Hub will cancel externally. '
           'Do not replace this test with a written explanation.'))
print('task_id=' + task['id'], flush=True)
observed = []
all_events = []
deadline = time.time() + 120
while time.time() < deadline:
    events = c.call('get_events', {'task_id': task['id'], 'limit': 200})['events']
    all_events = events
    roots = [e['data']['pid'] for e in events if e['kind'] == 'process']
    if roots:
        output = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
            'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress'],
            capture_output=True, encoding='utf-8', errors='replace', timeout=20).stdout
        rows = json.loads(output)
        descendants = set(roots)
        for _ in range(20):
            found = {p['ProcessId'] for p in rows if p['ParentProcessId'] in descendants}
            if found <= descendants:
                break
            descendants |= found
        sleeping = [p for p in rows if p['ProcessId'] in descendants and
                    p['Name'].lower() in ('powershell.exe', 'pwsh.exe') and
                    'start-sleep' in (p['CommandLine'] or '').lower()]
        if sleeping:
            observed = [{'pid': p['ProcessId'], 'name': p['Name']} for p in rows
                        if p['ProcessId'] in descendants]
            break
    if c.call('get_result', {'task_id': task['id']})['ready']:
        break
    time.sleep(1)
started = time.monotonic()
c.call('cancel_task', {'task_id': task['id']})
result = {'ready': False}
for _ in range(100):
    result = c.call('get_result', {'task_id': task['id']})
    if result['ready']:
        break
    time.sleep(.1)
import win32api, win32con, win32process
remaining = []
for process in observed:
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, process['pid'])
    except Exception:
        continue
    try:
        if win32process.GetExitCodeProcess(handle) == 259:
            remaining.append(process)
    finally:
        handle.Close()
summary = dict(task_id=task['id'], observed=observed, remaining=remaining,
               elapsed=round(time.monotonic()-started, 2), result=result,
               passed=bool(observed and not remaining and result.get('state') == 'cancelled'))
(root / 'child-cancel-acceptance-20261010.json').write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False), flush=True)
if not summary['passed']:
    raise SystemExit('Real shell-child cancellation not verified')
