import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .config import adapters, private_dir
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
        def do_GET(self):
            if not self.authorized(): return self.respond(401, {'error': 'UNAUTHORIZED'})
            if self.path != '/health': return self.respond(404, {'error': 'NOT_FOUND'})
            return self.respond(200, {'ok': True, 'pid': os.getpid()})
        def do_POST(self):
            if not self.authorized(): return self.respond(401, {'error': 'UNAUTHORIZED'})
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
                self.respond(200, {'error': {'code': error.code, 'message': str(error)}})
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
        service = Service(root, adapters(root))
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
