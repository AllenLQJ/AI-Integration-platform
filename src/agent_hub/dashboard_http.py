"""Loopback-only HTTP boundary for the read-only dashboard.

The management bearer mints single-use launch tickets. Browser sessions are
separate credentials and are never consulted by RPC or the legacy workbench.
"""
import json
import re
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import HubError


STATIC_ROOT = Path(__file__).with_name('dashboard_static')
ASSETS = {'index.html': 'text/html', 'app.mjs': 'text/javascript',
          'graph.mjs': 'text/javascript', 'timeline.mjs': 'text/javascript',
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

    @staticmethod
    def send(handler, status, body, content_type='application/json', cookie=None):
        content = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        handler.send_response(status)
        handler.send_header('Content-Type', content_type + '; charset=utf-8')
        handler.send_header('Content-Length', str(len(content)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Security-Policy', CSP)
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        if cookie:
            handler.send_header('Set-Cookie', cookie)
        handler.end_headers()
        handler.wfile.write(content)

    def error(self, handler, status, code):
        # Do not reflect URLs, native tokens, exception text or filesystem paths.
        self.send(handler, status, {'error': {'code': code, 'message': code}})

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
        body = json.loads(handler.rfile.read(size))
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
            from .dashboard import Dashboard
            from .dashboard_chains import DashboardChains
            dashboard = Dashboard(self.service)
            chains = DashboardChains(dashboard)
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
            elif re.fullmatch(r'/dashboard/api/v1/chains/([A-Za-z0-9_-]{1,128})', path):
                self.query(query, set())
                result = chains.detail(path.rsplit('/', 1)[1])
            else:
                match = re.fullmatch(r'/dashboard/api/v1/tasks/([A-Za-z0-9_-]{1,128})(/events)?', path)
                if not match:
                    return self.error(handler, 404, 'NOT_FOUND')
                if match[2]:
                    args = self.query(query, {'after_seq'})
                    after = self.integer(args.get('after_seq', '0'), 0, 9223372036854775807)
                    result = dashboard.events(match[1], after_seq=after)
                else:
                    self.query(query, set())
                    result = dashboard.detail(match[1])
            return self.send(handler, 200, result)
        name = 'index.html' if path in {'/dashboard', '/dashboard/'} else path.removeprefix('/dashboard/')
        if name not in ASSETS:
            return self.error(handler, 404, 'NOT_FOUND')
        try:
            content = self.asset(name).read_bytes()
        except OSError:
            return self.error(handler, 503, 'DASHBOARD_ASSETS_MISSING')
        return self.send(handler, 200, content, ASSETS[name])
