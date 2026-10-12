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
    sessions = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, status, body):
            content = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
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
    left, right = [], []
    for attempt in item['attempts']:
        ident = escape(attempt['task_id'])
        left.append(f'<section data-task="{ident}"><h3>尝试 {attempt["attempt"]} {ident}</h3><p>执行状态：{escape(attempt["state"])}</p>')
        for event in attempt['events']:
            left.append('<pre>' + dump(event) + '</pre>')
        left.append('<p>执行器未提供的字段标为缺失；进度文字不构成验收证据。</p></section>')
        right.append('<section data-verification="' + ident + '"><h3>尝试 ' + str(attempt['attempt']) + '</h3><h4>验收条件</h4><pre>' + dump(attempt['criteria']) + '</pre><h4>结论与原因</h4><pre>' + dump(attempt['verification']) + '</pre><pre class="live-verdict"></pre><h4>产物逻辑 ID 与哈希</h4><pre class="artifacts">' + dump(attempt['artifacts']) + '</pre></section>')
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><title>Agent Hub 工作台</title>'
            '<style>main{display:grid;grid-template-columns:1fr 1fr;gap:24px}pre{white-space:pre-wrap;overflow-wrap:anywhere}body{font-family:system-ui;padding:24px}</style>'
            '<body data-work="' + escape(item.get('view_task_id', item['root_task_id'])) + '"><h1>工作项 ' + escape(item['root_task_id']) + '</h1><p>验收：' + escape(item['work_status']) + '</p>'
            '<main><div id="timeline"><h2>执行时间线</h2>' + ''.join(left) + '</div><div><h2>验收</h2>' + ''.join(right) + '</div></main>'
            + '<script>for(const section of document.querySelectorAll("[data-task]")){let after=0;for(const p of section.querySelectorAll("pre")){try{after=Math.max(after,JSON.parse(p.textContent).seq)}catch{}}'
            'async function poll(){try{const r=await fetch("/work/"+encodeURIComponent(document.body.dataset.work)+"/events?attempt_id="+encodeURIComponent(section.dataset.task)+"&after_seq="+after,{credentials:"same-origin"});if(!r.ok)return;const data=await r.json();for(const e of data.events){if(e.seq<=after)continue;const p=document.createElement("pre");p.textContent=JSON.stringify(e,null,2);section.append(p);if(e.kind==="session"&&e.data.new_attempt_id){location.reload();return}if(e.kind==="artifact"){for(const v of document.querySelectorAll("[data-verification]")){if(v.dataset.verification===section.dataset.task)v.querySelector(".artifacts").textContent+="\\n"+JSON.stringify(e.data,null,2)}}if(e.kind==="verification"){for(const v of document.querySelectorAll("[data-verification]")){if(v.dataset.verification===section.dataset.task)v.querySelector(".live-verdict").textContent=JSON.stringify(e.data,null,2)}}after=e.seq}}catch{}setTimeout(poll,500)}poll()}</script></html>')
