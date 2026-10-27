import json
import logging
import os
import secrets
import socketserver
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


class HubHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, handler, dashboard):
        self.dashboard = dashboard
        super().__init__(address, handler)

    def server_bind(self):
        # HTTPServer.server_bind resolves the host's FQDN with a reverse DNS lookup,
        # which can block for a long time on macOS hosts without working DNS. The
        # daemon only listens on loopback and never uses server_name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = 'localhost', self.server_address[1]

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.dashboard.close()


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


WORK_SESSION_LIMIT = 1024
WORKBENCH_PAGE = 200
LATEST_SEQ = 9223372036854775807


def work_cookie_name(server):
    # Browsers scope cookies by host/path, not port; isolate local daemons.
    return 'hub_work_' + str(server.server_port)


def make_server(service, token):
    from .dashboard_http import DashboardHTTP, SESSION_TTL
    dashboard = DashboardHTTP(service)
    # cookie -> (task_id, monotonic expiry); bounded like dashboard sessions.
    sessions = {}
    sessions_lock = threading.Lock()

    def prune_sessions():
        now = time.monotonic()
        for value, (_, expiry) in list(sessions.items()):
            if expiry <= now:
                del sessions[value]

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
                # Port-scoped name first; legacy 'hub_work' keeps older clients working.
                cookie = cookies.get(work_cookie_name(self.server)) or cookies.get('hub_work')
            except Exception:
                return False
            if not cookie:
                return False
            with sessions_lock:
                prune_sessions()
                entry = sessions.get(cookie.value)
            return bool(entry and entry[0] == task_id)
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
                        if 'before_seq' in query:
                            if 'after_seq' in query:
                                raise ValueError()
                            before = int(query['before_seq'][0])
                            if not 1 <= before <= LATEST_SEQ:
                                raise ValueError()
                            return self.respond(200, service.store.events_before(attempt_id, before, WORKBENCH_PAGE))
                        after = int(query.get('after_seq', ['0'])[0])
                        if not 0 <= after <= LATEST_SEQ:
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
                        # Long attempts render their latest page; older pages load on demand.
                        page = service.store.events_before(attempt['task_id'], LATEST_SEQ, WORKBENCH_PAGE)
                        attempt['events'], attempt['events_more'] = page['events'], page['has_more']
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
                with sessions_lock:
                    prune_sessions()
                    if len(sessions) >= WORK_SESSION_LIMIT:
                        return self.respond(429, {'error': 'TOO_MANY_SESSIONS'})
                    sessions[cookie] = (parts[1], time.monotonic() + SESSION_TTL)
                self.send_response(204)
                self.send_header('Set-Cookie', work_cookie_name(self.server) + '=' + cookie
                                 + '; HttpOnly; Path=/work; SameSite=Strict; Max-Age='
                                 + str(SESSION_TTL))
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
                logging.getLogger(__name__).exception('RPC 内部错误')
                self.respond(500, {'error': {'code': 'INTERNAL_ERROR', 'message': '服务内部错误'}})
    return HubHTTPServer(('127.0.0.1', 0), Handler, dashboard)


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
        # The summary is CSS-generated content, so each <pre> stays the event's exact JSON.
        events = ''.join('<pre tabindex="0" data-seq="' + escape(str(event['seq'])) + '" data-kind="' + escape(str(event.get('kind', '')))
                         + '" data-summary="' + escape(event_summary(event)) + '">' + dump(event) + '</pre>' for event in attempt['events'])
        more = bool(attempt.get('events_more'))
        older = ('<button type="button" class="older"' + ('' if more else ' hidden') + '>加载更早事件</button>')
        left.append(f'<section data-task="{ident}" data-current="{flag}"><h3>尝试 {attempt["attempt"]} {ident}</h3>'
                    f'<p>执行状态：<span class="exec-state">{escape(attempt["state"])}</span></p>'
                    '<p class="note">执行器未提供的字段标为缺失；进度文字不构成验收证据。点击事件展开原始记录。' + older + '</p>'
                    '<div class="events" data-more="' + ('true' if more else 'false') + '">' + events + '</div></section>')
        right.append(f'<section data-verification="{ident}" data-current="{flag}"><h3>尝试 {attempt["attempt"]}</h3>'
                     f'<p>验收状态：<span class="verification-state">{escape(attempt["verification"]["state"])}</span></p>'
                     '<h4>验收条件</h4><pre>' + dump(attempt['criteria']) + '</pre>'
                     '<h4>当前结论与原因</h4><pre class="verdict">' + dump(attempt['verification']) + '</pre>'
                     '<h4>产物逻辑 ID 与哈希</h4><pre class="artifacts">' + dump(unique_artifacts(attempt['artifacts'])) + '</pre></section>')
    actions = '；'.join(item.get('pending_actions') or []) or '无'
    return ('<!doctype html><html lang="zh"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Hub 工作台</title>'
            '<style>' + WORKBENCH_STYLE + '</style>'
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


_STATIC = Path(__file__).with_name('dashboard_static')
# Authored as real files so they can be edited and tested directly; the page still inlines them.
# The script must not contain a backslash or "</" so it can sit inside <script> unchanged.
WORKBENCH_STYLE = (_STATIC / 'workbench.css').read_text(encoding='utf-8')
WORKBENCH_SCRIPT = (_STATIC / 'workbench.mjs').read_text(encoding='utf-8')


def event_summary(event):
    """One-line, human-readable label for an event; mirrors summary() in WORKBENCH_SCRIPT."""
    kind = str(event.get('kind') or '事件')
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    detail = ''
    for field in ('message', 'text', 'state', 'name', 'reason', 'status'):
        value = data.get(field)
        if isinstance(value, str) and value:
            detail = value
            break
    if kind == 'tool' and isinstance(data.get('name'), str):
        detail = data['name'] + (' · ' + data['status'] if isinstance(data.get('status'), str) else '')
    label = '#' + str(event.get('seq')) + ' ' + WORKBENCH_KINDS.get(kind, kind)
    detail = ' '.join(detail.split())
    return label + ('：' + (detail[:117] + '…' if len(detail) > 120 else detail) if detail else '')


WORKBENCH_KINDS = {'state': '状态', 'message': '消息', 'tool': '工具', 'progress': '进度', 'artifact': '产物',
                   'verification': '验收', 'review': '审核', 'session': '会话', 'activity': '操作', 'process': '进程'}


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
