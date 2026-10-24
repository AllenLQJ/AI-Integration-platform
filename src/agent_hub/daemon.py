import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, parse_qs
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .config import adapters, private_dir, scheduler_options
from .models import HubError
from .service import Service


class InstanceLock:
    def __init__(self, root):
        self.file = (Path(root) / 'daemon.lock').open('a+b')
        self.file.seek(0); self.file.write(b'0'); self.file.flush(); self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise HubError('ALREADY_RUNNING', '同一数据目录已有后台服务')
    def close(self):
        self.file.close()


def make_server(service, token):
    from .dashboard_http import DashboardHTTP
    dashboard = DashboardHTTP(service)
    sessions = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, status, body, no_store=False):
            content = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            if no_store:
                self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(content)
        def authorized(self):
            return (self.client_address[0] == '127.0.0.1' and not self.headers.get('Origin')
                    and secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token))
        def work_authorized(self, task_id):
            if self.authorized():
                return True
            if self.client_address[0] != '127.0.0.1' or self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return False
            try:
                cookies = SimpleCookie(self.headers.get('Cookie', ''))
                cookie = cookies.get('hub_work')
                return bool(cookie and sessions.get(cookie.value) == task_id)
            except Exception:
                return False
        def do_GET(self):
            if dashboard.handle(self, 'GET'):
                return
            path = urlsplit(self.path)
            parts = path.path.strip('/').split('/')
            if len(parts) in {2, 3} and parts[0] == 'work':
                if not self.work_authorized(parts[1]):
                    return self.respond(401, {'error': 'UNAUTHORIZED'})
                try:
                    task_id = parts[1]
                    if len(parts) == 3 and parts[2] == 'events':
                        query = parse_qs(path.query)
                        attempt_id = query.get('attempt_id', [task_id])[0]
                        if attempt_id != task_id and service.verification.root_id(attempt_id) != service.verification.root_id(task_id):
                            return self.respond(401, {'error': 'UNAUTHORIZED'})
                        after = int(query.get('after_seq', ['0'])[0])
                        if not 0 <= after <= 9223372036854775807:
                            raise ValueError()
                        deadline = time.monotonic() + 10
                        while True:
                            result = service.call('get_events', dict(task_id=attempt_id, after_seq=after, limit=200))
                            if result['events'] or time.monotonic() >= deadline or service.stopping.is_set():
                                return self.respond(200, result)
                            service.stopping.wait(.1)
                    if len(parts) == 3 and parts[2] == 'snapshot':
                        item = service.call('get_work_item', dict(task_id=task_id))
                        item['view_task_id'] = task_id
                        return self.respond(200, workbench_snapshot(item), no_store=True)
                    if len(parts) != 2:
                        return self.respond(404, {'error': 'NOT_FOUND'})
                    item = service.call('get_work_item', dict(task_id=task_id))
                    item['view_task_id'] = task_id
                    for attempt in item['attempts']:
                        cursor, events = 0, []
                        while True:
                            batch = service.call('get_events', dict(task_id=attempt['task_id'], after_seq=cursor, limit=200))
                            events.extend(batch['events'])
                            cursor = batch['next_seq']
                            if len(batch['events']) < 200:
                                break
                        attempt['events'] = events
                    content = render_workbench(item).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(content)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers(); self.wfile.write(content)
                    return
                except HubError as error:
                    return self.respond(404, {'error': error.payload()})
                except (ValueError, TypeError):
                    return self.respond(400, {'error': 'INVALID_ARGUMENT'})
            if not self.authorized(): return self.respond(401, {'error': 'UNAUTHORIZED'})
            if self.path != '/health': return self.respond(404, {'error': 'NOT_FOUND'})
            return self.respond(200, {'ok': True, 'pid': os.getpid()})
        def do_POST(self):
            if dashboard.handle(self, 'POST'):
                return
            if not self.authorized(): return self.respond(401, {'error': 'UNAUTHORIZED'})
            parts = urlsplit(self.path).path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == 'work' and parts[2] == 'session':
                try:
                    service.call('get_work_item', dict(task_id=parts[1]))
                except HubError as error:
                    return self.respond(404, {'error': error.payload()})
                cookie = secrets.token_urlsafe(32)
                sessions[cookie] = parts[1]
                self.send_response(204)
                self.send_header('Set-Cookie', 'hub_work=' + cookie + '; HttpOnly; Path=/work; SameSite=Strict')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/shutdown':
                self.respond(200, {'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != '/rpc': return self.respond(404, {'error': 'NOT_FOUND'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 2 * 1024 * 1024: raise ValueError('invalid size')
                request = json.loads(self.rfile.read(size))
                result = service.call(request['tool'], request.get('arguments', {}))
                self.respond(200, {'result': result})
            except HubError as error:
                self.respond(200, {'error': error.payload()})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {'error': {'code': 'INVALID_ARGUMENT', 'message': '请求格式不正确'}})
            except Exception:
                self.respond(500, {'error': {'code': 'INTERNAL_ERROR', 'message': '服务内部错误'}})
    return ThreadingHTTPServer(('127.0.0.1', 0), Handler)


def run(root):
    root = Path(root)
    private_dir(root)
    try: lock = InstanceLock(root)
    except HubError: return
    service = server = None
    descriptor = root / 'connection.json'
    token = secrets.token_urlsafe(32)
    try:
        service = Service(root, adapters(root), **scheduler_options(root))
        server = make_server(service, token)
        temporary = root / 'connection.tmp'
        temporary.write_text(json.dumps({'url': 'http://127.0.0.1:' + str(server.server_port),
                                          'token': token, 'pid': os.getpid()}), encoding='utf-8')
        if os.name != 'nt': temporary.chmod(0o600)
        temporary.replace(descriptor)
        server.serve_forever()
    finally:
        if server: server.server_close()
        if service: service.close()
        if descriptor.exists(): descriptor.unlink()
        lock.close()


def render_workbench(item):
    from .verification import safe
    from .store import Store
    item = safe(Store.safe(item))
    def dump(value):
        return escape(json.dumps(value, ensure_ascii=False, indent=2))
    current = next((a for a in item['attempts'] if a['task_id'] == item['current_task_id']), None)
    left, right = [], []
    for attempt in item['attempts']:
        ident = escape(attempt['task_id'])
        flag = 'true' if attempt['task_id'] == item['current_task_id'] else 'false'
        events = ''.join('<pre data-seq="' + escape(str(event['seq'])) + '">' + dump(event) + '</pre>' for event in attempt['events'])
        left.append(f'<section data-task="{ident}" data-current="{flag}"><h3>尝试 {attempt["attempt"]} {ident}</h3>'
                    f'<p>执行状态：<span class="exec-state">{escape(attempt["state"])}</span></p>'
                    '<p class="note">执行器未提供的字段标为缺失；进度文字不构成验收证据。</p>'
                    '<div class="events">' + events + '</div></section>')
        right.append(f'<section data-verification="{ident}" data-current="{flag}"><h3>尝试 {attempt["attempt"]}</h3>'
                     f'<p>验收状态：<span class="verification-state">{escape(attempt["verification"]["state"])}</span></p>'
                     '<h4>验收条件</h4><pre>' + dump(attempt['criteria']) + '</pre>'
                     '<h4>当前结论与原因</h4><pre class="verdict">' + dump(attempt['verification']) + '</pre>'
                     '<h4>产物逻辑 ID 与哈希</h4><pre class="artifacts">' + dump(unique_artifacts(attempt['artifacts'])) + '</pre></section>')
    actions = '；'.join(item.get('pending_actions') or []) or '无'
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><title>Agent Hub 工作台</title>'
            '<style>main{display:grid;grid-template-columns:1fr 1fr;gap:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}body{font-family:system-ui;padding:24px}'
            '#status{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px}#status dd{margin:0}.note{color:#555}'
            '#connection[data-state=down],#connection[data-state=unauthorized]{color:#b00020;font-weight:bold}</style>'
            '<body data-work="' + escape(item.get('view_task_id', item['root_task_id'])) + '" data-current="' + escape(item['current_task_id']) + '">'
            '<h1>工作项 ' + escape(item['root_task_id']) + '</h1><dl id="status">'
            '<dt>工作状态</dt><dd id="work-status">' + escape(item['work_status']) + '</dd>'
            '<dt>当前尝试</dt><dd id="current-attempt">' + escape(item['current_task_id']) + '</dd>'
            '<dt>当前尝试执行状态</dt><dd id="current-state">' + escape(current['state'] if current else '缺失') + '</dd>'
            '<dt>当前尝试验收状态</dt><dd id="verification-status">' + escape(current['verification']['state'] if current else '缺失') + '</dd>'
            '<dt>待处理</dt><dd id="pending-actions">' + escape(actions) + '</dd></dl>'
            '<p id="connection" role="status" data-state="connecting">正在建立实时同步</p>'
            '<p class="note">工作状态取当前尝试的综合状态。执行状态 succeeded 只表示执行结束，不等于验收通过；验收结论以验收状态为准，执行成功结束前验收状态为 not_requested。</p>'
            '<main><div id="timeline"><h2>执行时间线</h2>' + ''.join(left) + '</div><div id="verification"><h2>验收</h2>' + ''.join(right) + '</div></main>'
            '<script>' + WORKBENCH_SCRIPT + '</script></html>')


def unique_artifacts(artifacts):
    unique = {}
    for artifact in artifacts:
        unique[artifact.get('artifact_id') or json.dumps(artifact, sort_keys=True)] = artifact
    return list(unique.values())


def workbench_snapshot(item):
    """当前工作项与每次尝试的最小状态快照；不含事件、会话、工作区路径与总控信息。"""
    from .verification import safe
    from .store import Store
    item = safe(Store.safe(item))
    attempts = [dict(task_id=a['task_id'], parent_id=a['parent_id'], attempt=a['attempt'], state=a['state'],
                     work_status=a['work_status'], verification=a['verification'],
                     artifacts=unique_artifacts(a['artifacts'])) for a in item['attempts']]
    current = next((a for a in attempts if a['task_id'] == item['current_task_id']), None)
    return dict(root_task_id=item['root_task_id'], view_task_id=item.get('view_task_id', item['root_task_id']),
                current_task_id=item['current_task_id'], work_status=item['work_status'],
                verification=current['verification'] if current else None,
                pending_actions=item.get('pending_actions') or [], attempts=attempts)


# 不含反斜杠与 "</"，可原样内联进 <script>。
WORKBENCH_SCRIPT = '''(function(){
"use strict";
const body=document.body,base="/work/"+encodeURIComponent(body.dataset.work);
const ACTIVE=new Set(["queued","running","waiting_for_user","cancelling"]);
const VERIFYING=new Set(["awaiting_verification","verifying","reviewing"]);
const REFRESH=new Set(["state","verification","verification_retry","session","artifact","review"]);
const attempts=new Map();
const snap={state:"connecting",detail:"",failures:0,last:null,timer:null,busy:false,dirty:false};
let current=body.dataset.current,reloading=false;
function one(selector,root){return (root||document).querySelector(selector)}
function put(selector,value,root){const node=one(selector,root);if(node&&node.textContent!==value)node.textContent=value}
function pretty(value){return JSON.stringify(value,null,2)}
function key(artifact){return artifact.artifact_id||pretty(artifact)}
function failed(state){return state==="down"||state==="unauthorized"}
function backoff(failures,status){return status===401?5000:Math.min(30000,1000*Math.pow(2,Math.min(failures-1,5)))}
function settled(a){return !ACTIVE.has(a.state)&&!VERIFYING.has(a.verification)}
function renderArtifacts(a){put(".artifacts",pretty(Array.from(a.artifacts.values())),a.panel)}
for(const section of document.querySelectorAll("[data-task]")){
  let panel=null;
  for(const candidate of document.querySelectorAll("[data-verification]"))if(candidate.dataset.verification===section.dataset.task)panel=candidate;
  const a={id:section.dataset.task,section:section,panel:panel,after:0,artifacts:new Map(),state:one(".exec-state",section).textContent,
           verification:one(".verification-state",panel).textContent,timer:null,busy:false,failure:null,failures:0};
  for(const node of one(".events",section).children)a.after=Math.max(a.after,Number(node.dataset.seq)||0);
  let initial=[];
  try{initial=JSON.parse(one(".artifacts",panel).textContent)}catch(error){initial=[]}
  for(const artifact of initial)a.artifacts.set(key(artifact),artifact);
  renderArtifacts(a);
  attempts.set(a.id,a);
}
function eventsChannel(){
  const channel={state:"ok",detail:"",failures:0};
  for(const a of attempts.values()){
    if(!a.failure)continue;
    channel.failures=Math.max(channel.failures,a.failures);
    if(a.failure.status===401){channel.state="unauthorized";channel.detail=a.failure.reason}
    else if(channel.state!=="unauthorized"){channel.state="down";channel.detail=a.failure.reason}
  }
  return channel;
}
function describe(name,channel,impact){
  return name+(channel.state==="unauthorized"?"未授权":"中断")+"（"+channel.detail+"，已失败 "+channel.failures+" 次）："+impact;
}
function showConnection(){
  const node=one("#connection"),events=eventsChannel(),time=snap.last?snap.last.toLocaleTimeString():"页面加载时";
  const state=snap.state==="unauthorized"||events.state==="unauthorized"?"unauthorized":
    failed(snap.state)||failed(events.state)?"down":snap.state==="connecting"?"connecting":"live";
  node.dataset.state=state;node.dataset.snapshot=snap.state;node.dataset.events=events.state;
  node.dataset.snapshotAt=snap.last?String(snap.last.getTime()):"";
  if(state==="connecting"){node.textContent="正在建立实时同步";return}
  if(state==="live"){node.textContent="实时同步中，最后状态快照 "+time;return}
  const parts=[];
  if(failed(snap.state))parts.push(describe("状态快照通道",snap,"顶部状态、执行状态与验收结论停留在 "+time+" 的快照，不代表最新进展"));
  if(failed(events.state))parts.push(describe("事件通道",events,"执行时间线与产物可能缺少最新事件"));
  node.textContent=(state==="unauthorized"?"未授权：":"连接中断：")+parts.join("；")+"。"+(state==="unauthorized"?
    "工作台会话已失效或无权访问此工作项，请由总控重新签发工作台会话（POST "+base+"/session）；会话恢复后本页会自动继续同步，也可重新签发后刷新页面。":
    "正在按退避间隔自动重连。");
}
async function load(url){
  let response;
  try{response=await fetch(url,{credentials:"same-origin",cache:"no-store"})}catch(error){return {error:0,reason:"网络错误"}}
  if(!response.ok)return {error:response.status,reason:"HTTP "+response.status};
  try{return {data:await response.json()}}catch(error){return {error:0,reason:"响应不完整"}}
}
function schedule(a,delay){
  if(reloading||a.timer||a.busy)return;
  if(delay===undefined)delay=a.id===current||!settled(a)?0:30000;
  a.timer=setTimeout(function(){a.timer=null;poll(a)},delay);
}
function setState(a,state){
  a.state=state;put(".exec-state",state,a.section);
  if(a.id===current)put("#current-state",state);
}
function eventsFailed(a,status,reason){
  a.failures+=1;a.failure={status:status,reason:reason};
  showConnection();
  schedule(a,backoff(a.failures,status));
}
async function poll(a){
  if(reloading||a.busy)return;
  a.busy=true;
  const result=await load(base+"/events?attempt_id="+encodeURIComponent(a.id)+"&after_seq="+a.after);
  a.busy=false;
  if(reloading)return;
  if(result.error!==undefined)return eventsFailed(a,result.error,result.reason);
  let refresh=false;
  try{
    for(const e of result.data.events){
      if(e.seq<=a.after)continue;
      a.after=e.seq;
      const node=document.createElement("pre");
      node.dataset.seq=String(e.seq);node.textContent=pretty(e);
      one(".events",a.section).append(node);
      if(e.kind==="state"&&e.data&&typeof e.data.state==="string")setState(a,e.data.state);
      if(e.kind==="artifact"&&e.data){a.artifacts.set(key(e.data),e.data);renderArtifacts(a)}
      if(REFRESH.has(e.kind))refresh=true;
    }
  }catch(error){return eventsFailed(a,0,"响应异常")}
  a.failure=null;a.failures=0;
  showConnection();
  if(refresh)requestSnapshot();
  schedule(a);
}
function apply(data){
  for(const s of data.attempts)if(!attempts.has(s.task_id)){reloading=true;location.reload();return}
  current=data.current_task_id;body.dataset.current=current;
  put("#work-status",data.work_status);put("#current-attempt",current);
  put("#pending-actions",data.pending_actions.length?data.pending_actions.join("；"):"无");
  for(const s of data.attempts){
    const a=attempts.get(s.task_id);
    a.verification=s.verification.state;
    setState(a,s.state);
    put(".verification-state",s.verification.state,a.panel);
    put(".verdict",pretty(s.verification),a.panel);
    a.artifacts=new Map();
    for(const artifact of s.artifacts)a.artifacts.set(key(artifact),artifact);
    renderArtifacts(a);
    a.section.dataset.current=a.panel.dataset.current=String(s.task_id===current);
  }
  const now=attempts.get(current);
  put("#current-state",now?now.state:"缺失");put("#verification-status",now?now.verification:"缺失");
}
function scheduleSnapshot(delay){
  if(reloading)return;
  if(snap.timer)clearTimeout(snap.timer);
  snap.timer=setTimeout(function(){snap.timer=null;snapshot()},delay);
}
function requestSnapshot(){
  if(failed(snap.state))return;
  if(snap.busy)snap.dirty=true;else scheduleSnapshot(0);
}
async function snapshot(){
  if(snap.busy){snap.dirty=true;return}
  snap.busy=true;snap.dirty=false;
  const result=await load(base+"/snapshot");
  snap.busy=false;
  if(reloading)return;
  let status=result.error,reason=result.reason;
  if(status===undefined){try{apply(result.data)}catch(error){status=0;reason="响应异常"}}
  if(reloading)return;
  if(status!==undefined){
    snap.state=status===401?"unauthorized":"down";snap.detail=reason;snap.failures+=1;
    showConnection();
    return scheduleSnapshot(backoff(snap.failures,status));
  }
  snap.state="ok";snap.detail="";snap.failures=0;snap.last=new Date();
  showConnection();
  if(snap.dirty)return scheduleSnapshot(0);
  let active=false;
  for(const a of attempts.values())if(!settled(a))active=true;
  scheduleSnapshot(active?3000:15000);
}
showConnection();
for(const a of attempts.values())schedule(a,0);
scheduleSnapshot(0);
})();'''
