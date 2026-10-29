"""Password-protected key management, shared by both HTTP servers."""
from datetime import datetime
from http.cookies import SimpleCookie, CookieError
import json
import logging
import sqlite3
from urllib.parse import urlsplit

from inferfabric.security.console_auth import ConsoleAuth, LoginError

log = logging.getLogger('inferfabric.security')
COOKIE = 'iff_console'
MAX_BODY = 16384


def _reply(h, data, status=200, cookie=None):
    # Do not use the generic JSON helper: its wildcard CORS is unsuitable here.
    body = json.dumps(data, ensure_ascii=False).encode('utf-8')
    h.send_response(status)
    h.send_header('Content-Type', 'application/json; charset=utf-8')
    h.send_header('Content-Length', str(len(body)))
    h.send_header('Cache-Control', 'no-store')
    h.send_header('X-Content-Type-Options', 'nosniff')
    h.send_header('Connection', 'close')
    if cookie:
        h.send_header('Set-Cookie', cookie)
    h.end_headers()
    h.wfile.write(body)


class SecurityAdmin:
    def __init__(self, store, *, origins):
        self.store = store
        self.console = ConsoleAuth(store)
        self.origins = {}
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                    or parsed.path or parsed.query or parsed.fragment or parsed.username
                    or parsed.password or (parsed.scheme == 'http' and parsed.hostname not in
                                           ('127.0.0.1', 'localhost', '::1'))):
                raise ValueError('console origins must be explicit HTTPS or loopback HTTP origins')
            if parsed.netloc in self.origins:
                raise ValueError('ambiguous console origin')
            self.origins[parsed.netloc] = origin
        if not self.origins:
            raise ValueError('console requires a trusted origin')

    def _body(self, h):
        if h.headers.get('Transfer-Encoding'):
            raise ValueError('chunked management requests are not supported')
        size = int(h.headers.get('Content-Length', '0'))
        if size < 0:
            raise ValueError('invalid content length')
        if size > MAX_BODY:
            raise OverflowError('request too large')
        raw = h.rfile.read(size)
        if len(raw) != size:
            raise ValueError('incomplete body')
        data = json.loads(raw or b'{}')
        if not isinstance(data, dict):
            raise ValueError('expected JSON object')
        return data

    def handle(self, h, action):
        origin = self.origins.get(h.headers.get('Host', ''))
        supplied = h.headers.get('Origin')
        write = h.command != 'GET'
        if origin is None or (write and supplied != origin) or (supplied is not None and supplied != origin):
            _reply(h, {'error': 'untrusted origin'}, 403)
            return
        try:
            cookies = SimpleCookie()
            cookies.load(h.headers.get('Cookie', ''))
            session = cookies[COOKIE].value if COOKIE in cookies else ''
            secure = '; Secure' if origin.startswith('https:') else ''
            suffix = '; Path=/admin/keys; HttpOnly; SameSite=Strict' + secure
            if action != 'login' and not self.console.validate(
                    session, h.headers.get('X-CSRF-Token'), write=write):
                _reply(h, {'error': 'console login required'}, 401)
                return
            data = self._body(h) if write else {}
            if action == 'login':
                session, csrf = self.console.login(data.get('password'))
                _reply(h, {'csrf': csrf, 'expires_in': 900}, cookie=f'{COOKIE}={session}; Max-Age=900' + suffix)
            elif action == 'logout':
                self.console.logout(session)
                _reply(h, {'status': 'ok'}, cookie=f'{COOKIE}=; Max-Age=0' + suffix)
            elif action == 'list':
                _reply(h, {'keys': self.store.list_keys(), 'policy': self.store.get_policy()})
            elif action == 'create':
                models = data.get('models')
                if models is not None and (not isinstance(models, list) or any(
                        not isinstance(m, str) or not m.startswith('iff/') or len(m) <= 4 for m in models)):
                    raise ValueError('scope must be full access or exact pool names')
                expiry = data.get('expires_at')
                if expiry is not None:
                    if not isinstance(expiry, str):
                        raise ValueError('invalid expiry')
                    expiry = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                entry, token = self.store.create_key(data.get('name'), models, expiry)
                log.info('API key created id=%s', entry['id'])
                _reply(h, {'entry': entry, 'key': token}, 201)
            elif action == 'revoke':
                key_id = data.get('id')
                if not isinstance(key_id, str) or len(key_id) != 32:
                    raise ValueError('invalid key id')
                self.store.revoke_key(key_id)
                log.info('API key revoked id=%s', key_id)
                _reply(h, {'status': 'ok'})
            elif action == 'policy':
                policy = data.get('policy')
                if data.get('confirm') != policy or (policy == 'all' and data.get('confirm_all') is not True):
                    raise ValueError('explicit policy confirmation required')
                self.store.set_policy(policy)
                log.info('API key enforcement changed policy=%s', policy)
                _reply(h, {'policy': policy})
            else:
                _reply(h, {'error': 'not found'}, 404)
        except LoginError as e:
            _reply(h, {'error': str(e)}, e.status)
        except OverflowError:
            _reply(h, {'error': 'request too large'}, 413)
        except (ValueError, TypeError, CookieError):
            _reply(h, {'error': 'invalid request'}, 400)
        except KeyError:
            _reply(h, {'error': 'key not found'}, 404)
        except sqlite3.IntegrityError:
            _reply(h, {'error': 'key name already exists'}, 409)
        except (sqlite3.DatabaseError, OSError):
            log.error('Security management store unavailable')
            _reply(h, {'error': 'security store unavailable'}, 503)


def handle(h, pm, action):
    service = getattr(pm, 'security_admin', None)
    if service is None:
        _reply(h, {'error': 'initialize security using iff keys init first'}, 503)
        return
    service.handle(h, action)
