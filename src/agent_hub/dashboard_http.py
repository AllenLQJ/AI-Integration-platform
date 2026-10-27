"""Loopback-only HTTP boundary for the read-only dashboard.

The management bearer mints single-use launch tickets. Browser sessions are
separate credentials and are never consulted by RPC or the legacy workbench.
"""
import json
import hashlib
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import HubError


STATIC_ROOT = Path(__file__).with_name('dashboard_static')
ASSETS = {'index.html': 'text/html', 'app.mjs': 'text/javascript',
          'graph.mjs': 'text/javascript', 'timeline.mjs': 'text/javascript', 'overview.mjs': 'text/javascript', 'viz.mjs': 'text/javascript', 'chainviz.mjs': 'text/javascript',
          'status_labels.mjs': 'text/javascript',
          'api.mjs': 'text/javascript', 'state.mjs': 'text/javascript', 'style.css': 'text/css'}
COOKIE_PREFIX = 'hub_dashboard_'
SESSION_TTL = 8 * 60 * 60
TICKET_TTL = 60
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class DashboardHTTP:
    def __init__(self, service):
        self.service = service
        self.tickets = {}
        self.sessions = {}
        self.lock = threading.Lock()
        self.revalidated = OrderedDict()
        self.result_files = {}
        self.monitor = None
        self.cache_epoch = secrets.token_hex(12)
        from .dashboard_chains import SnapshotCache
        self.snapshots = SnapshotCache(self.data_version)

    def data_version(self):
        # data_version is meaningful across commits only on the SAME connection.
        # A read-only, autocommit observer notices every DB writer, including
        # verification changes that do not touch tasks.updated or append events.
        with self.lock:
            if self.monitor is None:
                self.monitor = sqlite3.connect(Path(self.service.store.path).resolve().as_uri() + '?mode=ro',
                                               uri=True, check_same_thread=False)
            return self.monitor.execute('PRAGMA data_version').fetchone()[0]

    @staticmethod
    def file_key(paths):
        from .store import Store
        signatures = [(path, Store.result_file_signature(path)) for path in paths]
        return hashlib.sha256(json.dumps(signatures, ensure_ascii=False).encode('utf-8')).hexdigest()[:16]

    def content_etag(self, route, content):
        # Content-addressed: writes to unrelated tasks bump data_version but leave
        # this projection, and therefore its ETag, unchanged.
        digest = hashlib.sha256(route.encode('utf-8') + b'\0' + content).hexdigest()[:32]
        return f'"{self.cache_epoch}-{digest}"'

    def fresh_etag(self, route):
        """The remembered ETag if nothing it depends on can have changed, else None."""
        version = self.data_version()
        with self.lock:
            entry = self.revalidated.get(route)
            paths = self.result_files.get(route, ())
        if not entry or entry[0] != version or entry[2] != self.file_key(paths):
            return version, None
        return version, entry[1]

    def remember(self, route, version, etag, files=()):
        files = tuple(files)
        file_key = self.file_key(files)
        with self.lock:
            self.revalidated[route] = (version, etag, file_key)
            self.result_files[route] = files
            self.revalidated.move_to_end(route)
            while len(self.revalidated) > 128:
                removed, _ = self.revalidated.popitem(last=False)
                self.result_files.pop(removed, None)

    def close(self):
        with self.lock:
            if self.monitor is not None:
                self.monitor.close()
                self.monitor = None
            self.revalidated.clear()
            self.result_files.clear()
        self.snapshots.clear()

    @staticmethod
    def send(handler, status, body, content_type='application/json', cookie=None, etag=None):
        content = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        handler.send_response(status)
        handler.send_header('Content-Type', content_type + '; charset=utf-8')
        if status != 304:
            handler.send_header('Content-Length', str(len(content)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Security-Policy', CSP)
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        if etag:
            handler.send_header('ETag', etag)
        if cookie:
            handler.send_header('Set-Cookie', cookie)
        handler.end_headers()
        handler.wfile.write(content)

    def error(self, handler, status, code):
        # Do not reflect URLs, native tokens, exception text or filesystem paths.
        self.send(handler, status, {'error': {'code': code, 'message': code}})
        if status in {401, 403}:
            self.discard_rejected_body(handler)

    @staticmethod
    def discard_rejected_body(handler):
        # Send the denial first. A closing socket with an unread POST body can
        # reset that response on Windows. Drain only a small, explicitly framed
        # body, under one deadline; never parse it or authorize any operation.
        if getattr(handler, 'command', None) != 'POST' or getattr(handler, '_dashboard_body_read', False):
            return
        lengths = handler.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or handler.headers.get('Transfer-Encoding') is not None:
            return
        try:
            remaining = int(lengths[0])
        except ValueError:
            return
        if not 0 < remaining <= 4096:
            return
        connection = handler.connection
        previous_timeout = connection.gettimeout()
        deadline = time.monotonic() + .2
        try:
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                connection.settimeout(timeout)
                block = handler.rfile.read1(remaining)
                if not block:
                    break
                remaining -= len(block)
        except OSError:
            pass
        finally:
            connection.settimeout(previous_timeout)

    @staticmethod
    def origin(handler):
        return 'http://127.0.0.1:' + str(handler.server.server_port)

    def provenance(self, handler, require_origin=False):
        expected = self.origin(handler)
        if handler.client_address[0] != '127.0.0.1':
            return False
        for name in ['Host', 'Origin', 'Referer', 'Sec-Fetch-Site']:
            values = handler.headers.get_all(name, [])
            if len(values) > 1 or (name == 'Host' and len(values) != 1):
                return False
        if handler.headers.get('Host') != expected.removeprefix('http://'):
            return False
        origin = handler.headers.get('Origin')
        if (require_origin and origin != expected) or (origin is not None and origin != expected):
            return False
        if handler.headers.get('Sec-Fetch-Site') not in {None, 'same-origin', 'none'}:
            return False
        referer = handler.headers.get('Referer')
        if referer:
            try:
                parsed = urlsplit(referer)
                if parsed.username or parsed.password or parsed.scheme + '://' + parsed.netloc != expected:
                    return False
            except ValueError:
                return False
        return True

    def prune(self):
        now = time.monotonic()
        for entries in (self.tickets, self.sessions):
            for value, expiry in list(entries.items()):
                if expiry <= now:
                    del entries[value]

    @staticmethod
    def cookie_name(handler):
        # Browsers scope cookies by host/path, not port; isolate local daemons.
        return COOKIE_PREFIX + str(handler.server.server_port)

    def authenticated(self, handler):
        if len(handler.headers.get_all('Cookie', [])) != 1:
            return False
        try:
            cookies = SimpleCookie(handler.headers.get('Cookie', ''))
            cookie = cookies.get(self.cookie_name(handler))
        except CookieError:
            return False
        with self.lock:
            self.prune()
            return bool(cookie and cookie.value in self.sessions)

    @staticmethod
    def asset(name):
        candidate = STATIC_ROOT / name
        # Fixed top-level names prevent traversal; resolve also detects directory
        # junctions on Windows, where is_symlink alone is insufficient.
        if (name not in ASSETS or STATIC_ROOT.is_symlink() or candidate.is_symlink()
                or STATIC_ROOT.resolve() != STATIC_ROOT.absolute()
                or candidate.resolve().parent != STATIC_ROOT.absolute()
                or not candidate.is_file()):
            raise FileNotFoundError(name)
        return candidate

    @staticmethod
    def json_body(handler):
        if (handler.headers.get('Transfer-Encoding') is not None
                or len(handler.headers.get_all('Content-Length', [])) != 1
                or len(handler.headers.get_all('Content-Type', [])) != 1
                or handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'):
            raise ValueError()
        size = int(handler.headers['Content-Length'])
        if not 0 < size <= 4096:
            raise ValueError()
        raw = handler.rfile.read(size)
        handler._dashboard_body_read = True
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        return body

    def handle(self, handler, method):
        """Return True iff this request belongs to the dashboard namespace."""
        raw_path = handler.path.split('?', 1)[0]
        if raw_path != '/dashboard' and not raw_path.startswith('/dashboard/'):
            return False
        try:
            if not self.provenance(handler):
                self.error(handler, 403, 'FORBIDDEN_ORIGIN')
                return True
            parsed = urlsplit(handler.path)
            path = parsed.path
            if method == 'POST':
                self.post(handler, path, parsed.query)
            elif method == 'GET':
                self.get(handler, path, parsed.query)
            else:
                self.error(handler, 405, 'METHOD_NOT_ALLOWED')
        except (ValueError, TypeError, UnicodeError):
            self.error(handler, 400, 'INVALID_ARGUMENT')
        except HubError as error:
            status = 404 if error.code in {'NOT_FOUND', 'TASK_NOT_FOUND'} else 400
            self.error(handler, status, error.code)
        except Exception:
            logging.getLogger(__name__).exception('看板请求内部错误')
            self.error(handler, 500, 'INTERNAL_ERROR')
        return True

    def post(self, handler, path, query):
        if query:
            raise ValueError()
        if path == '/dashboard/ticket':
            if handler.headers.get('Origin') is not None:
                return self.error(handler, 403, 'FORBIDDEN_ORIGIN')
            if not handler.authorized():
                return self.error(handler, 401, 'UNAUTHORIZED')
            self.json_body(handler)
            try:
                for name in ASSETS:
                    self.asset(name)
            except OSError:
                return self.error(handler, 503, 'DASHBOARD_ASSETS_MISSING')
            ticket = secrets.token_urlsafe(32)
            with self.lock:
                self.prune()
                if len(self.tickets) >= 1024:
                    return self.error(handler, 429, 'TOO_MANY_TICKETS')
                self.tickets[ticket] = time.monotonic() + TICKET_TTL
            return self.send(handler, 200, {'ticket': ticket, 'url': self.origin(handler) + '/dashboard/#ticket=' + ticket})
        if path == '/dashboard/session':
            if not self.provenance(handler, require_origin=True):
                return self.error(handler, 403, 'FORBIDDEN_ORIGIN')
            body = self.json_body(handler)
            ticket = body.get('ticket')
            if set(body) != {'ticket'} or not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket):
                raise ValueError()
            with self.lock:
                self.prune()
                if ticket not in self.tickets:
                    return self.error(handler, 401, 'UNAUTHORIZED')
                if len(self.sessions) >= 1024:
                    return self.error(handler, 429, 'TOO_MANY_SESSIONS')
                del self.tickets[ticket]
                cookie = secrets.token_urlsafe(32)
                self.sessions[cookie] = time.monotonic() + SESSION_TTL
            return self.send(handler, 200, {'ok': True}, cookie=(self.cookie_name(handler) + '=' + cookie
                + '; HttpOnly; Path=/dashboard; SameSite=Strict; Max-Age=' + str(SESSION_TTL)))
        return self.error(handler, 404, 'NOT_FOUND')

    @staticmethod
    def query(query, allowed):
        values = parse_qs(query, keep_blank_values=True, max_num_fields=10, errors='strict')
        if set(values) - set(allowed) or any(len(items) != 1 for items in values.values()):
            raise ValueError()
        return {key: items[0] for key, items in values.items()}

    @staticmethod
    def integer(value, minimum, maximum):
        if not re.fullmatch(r'[0-9]{1,19}', value):
            raise ValueError()
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError()
        return number

    def get(self, handler, path, query):
        if path.startswith('/dashboard/api/'):
            if not self.authenticated(handler):
                return self.error(handler, 401, 'UNAUTHORIZED')
            conditional = bool(re.fullmatch(r'/dashboard/api/v1/(?:(?:chains|tasks)(?:/[A-Za-z0-9_-]{1,128})?|overview)', path))
            requested = handler.headers.get('If-None-Match')
            version, fresh = self.fresh_etag(handler.path) if conditional else (None, None)
            if fresh and requested == fresh:
                return self.send(handler, 304, b'', etag=fresh)
            from .dashboard import Dashboard
            from .dashboard_chains import DashboardChains
            dashboard = Dashboard(self.service)
            chains = DashboardChains(dashboard, snapshots=self.snapshots)
            if path in {'/dashboard/api/v1/tasks', '/dashboard/api/v1/chains'}:
                args = self.query(query, {'limit', 'offset', 'q', 'state', 'agent'})
                limit = self.integer(args.get('limit', '30'), 1, 100)
                offset = self.integer(args.get('offset', '0'), 0, 9223372036854775807)
                if any(len(args.get(key, '')) > bound for key, bound in [('q', 500), ('state', 100), ('agent', 200)]):
                    raise ValueError()
                listing = chains.chains if path.endswith('/chains') else dashboard.tasks
                result = listing(limit=limit, offset=offset, q=args.get('q', ''),
                                         state=args.get('state', ''), agent=args.get('agent', ''))
            elif path == '/dashboard/api/v1/agents':
                self.query(query, set())
                result = dashboard.agents()
            elif path == '/dashboard/api/v1/overview':
                self.query(query, set())
                result = chains.overview()
            elif re.fullmatch(r'/dashboard/api/v1/chains/([A-Za-z0-9_-]{1,128})', path):
                self.query(query, set())
                result = chains.detail(path.rsplit('/', 1)[1])
            else:
                match = re.fullmatch(r'/dashboard/api/v1/tasks/([A-Za-z0-9_-]{1,128})(/events)?', path)
                if not match:
                    return self.error(handler, 404, 'NOT_FOUND')
                if match[2]:
                    args = self.query(query, {'after_seq', 'before_seq'})
                    if 'before_seq' in args:
                        if 'after_seq' in args:
                            raise ValueError()
                        before = self.integer(args['before_seq'], 1, 9223372036854775807)
                        result = dashboard.events(match[1], before_seq=before)
                    else:
                        after = self.integer(args.get('after_seq', '0'), 0, 9223372036854775807)
                        result = dashboard.events(match[1], after_seq=after)
                else:
                    # after_seq folds the live event page into the detail poll (one request per tick).
                    args = self.query(query, {'after_seq'})
                    result = dashboard.detail(match[1])
                    if 'after_seq' in args:
                        after = self.integer(args['after_seq'], 0, 9223372036854775807)
                        result['live_events'] = dashboard.events(match[1], after_seq=after)
            if not conditional:
                return self.send(handler, 200, result)
            content = json.dumps(result, ensure_ascii=False).encode('utf-8')
            etag = self.content_etag(handler.path, content)
            # Only skip future projections at this version when no writer committed
            # while it was read; the content ETag itself is always accurate.
            # The overview's day window moves at midnight without any write, so it is
            # always recomputed; its content ETag still yields 304 when nothing changed.
            if (version == self.data_version() and dashboard.results_unchanged()
                    and path != '/dashboard/api/v1/overview'):
                self.remember(handler.path, version, etag, dashboard.result_files)
            if requested == etag:
                return self.send(handler, 304, b'', etag=etag)
            return self.send(handler, 200, content, etag=etag)
        name = 'index.html' if path in {'/dashboard', '/dashboard/'} else path.removeprefix('/dashboard/')
        if name not in ASSETS:
            return self.error(handler, 404, 'NOT_FOUND')
        try:
            content = self.asset(name).read_bytes()
        except OSError:
            return self.error(handler, 503, 'DASHBOARD_ASSETS_MISSING')
        return self.send(handler, 200, content, ASSETS[name])
