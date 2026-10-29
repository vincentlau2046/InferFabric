import io
import json
from types import SimpleNamespace

import pytest

from inferfabric.security.admin import SecurityAdmin
from inferfabric.security.console_auth import set_password
from inferfabric.security.key_store import KeyStore


class Handler:
    def __init__(self, body=None, headers=None, method='POST'):
        raw = json.dumps(body or {}).encode()
        self.headers = {'Host': '127.0.0.1:18099', 'Origin': 'http://127.0.0.1:18099',
                        'Content-Length': str(len(raw)), **(headers or {})}
        self.command = method
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.response_headers = {}
        self.status = None
    def send_response(self, status):
        self.status = status
    def send_header(self, key, value):
        self.response_headers[key] = value
    def end_headers(self):
        pass
    def response(self):
        return json.loads(self.wfile.getvalue())


@pytest.fixture
def admin(tmp_path):
    store = KeyStore(tmp_path / 'security.db', provision=True)
    set_password(store, 'test-password-long')
    return SecurityAdmin(store, origins=['http://127.0.0.1:18099'])


def login(admin):
    h = Handler({'password': 'test-password-long'})
    admin.handle(h, 'login')
    assert h.status == 200
    cookie = h.response_headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
    assert 'no-store' in h.response_headers['Cache-Control']
    assert 'Access-Control-Allow-Origin' not in h.response_headers
    return {'Cookie': cookie.split(';')[0], 'X-CSRF-Token': h.response()['csrf']}


@pytest.mark.parametrize('headers', [{'Origin': 'http://evil.test'}, {'Origin': ''},
                                      {'Host': 'evil.test'}, {'Origin': 'null'}])
def test_login_cross_origin_denied(admin, headers):
    h = Handler({'password': 'test-password-long'}, headers)
    admin.handle(h, 'login')
    assert h.status == 403


def test_create_list_revoke_and_policy(admin):
    headers = login(admin)
    h = Handler({'name': 'client', 'models': ['iff/work']}, headers)
    admin.handle(h, 'create')
    assert h.status == 201
    token, meta = h.response()['key'], h.response()['entry']
    assert admin.store.check(token, 'iff/work')[0]
    h = Handler(headers=headers, method='GET')
    admin.handle(h, 'list')
    assert h.status == 200 and token not in h.wfile.getvalue().decode()
    assert h.response()['policy'] == 'off'
    h = Handler({'policy': 'all', 'confirm': 'all'}, headers)
    admin.handle(h, 'policy')
    assert h.status == 400
    h = Handler({'policy': 'all', 'confirm': 'all', 'confirm_all': True}, headers)
    admin.handle(h, 'policy')
    assert h.status == 200 and admin.store.get_policy() == 'all'
    h = Handler({'id': meta['id']}, headers)
    admin.handle(h, 'revoke')
    assert h.status == 200 and not admin.store.check(token, 'iff/work')[0]


def test_csrf_and_anonymous_rejected(admin):
    h = Handler({'name': 'client'})
    admin.handle(h, 'create')
    assert h.status == 401
    headers = login(admin)
    headers.pop('X-CSRF-Token')
    h = Handler({'name': 'client'}, headers)
    admin.handle(h, 'create')
    assert h.status == 401
    assert admin.store.list_keys() == []


def test_body_bounds_and_validation(admin):
    headers = login(admin)
    for body in ({'name': ''}, {'name': 'x', 'models': []},
                 {'name': 'x', 'models': ['direct-model']},
                 {'name': 'x', 'expires_at': 'bad'}, {'name': 'x', 'expires_at': 12}):
        h = Handler(body, headers)
        admin.handle(h, 'create')
        assert h.status == 400
    h = Handler({}, {**headers, 'Content-Length': '100000'})
    admin.handle(h, 'create')
    assert h.status == 413
    assert admin.store.list_keys() == []


def test_logout_and_secure_cookie(admin):
    headers = login(admin)
    h = Handler({}, headers)
    admin.handle(h, 'logout')
    assert h.status == 200 and 'Max-Age=0' in h.response_headers['Set-Cookie']
    h = Handler(headers=headers, method='GET')
    admin.handle(h, 'list')
    assert h.status == 401
    tls = SecurityAdmin(admin.store, origins=['https://console.example'])
    h = Handler({'password': 'test-password-long'}, {'Host': 'console.example', 'Origin': 'https://console.example'})
    tls.handle(h, 'login')
    assert h.status == 200 and '; Secure' in h.response_headers['Set-Cookie']


def test_audit_redacted(admin, caplog):
    headers = login(admin)
    with caplog.at_level('INFO'):
        h = Handler({'name': 'client'}, headers)
        admin.handle(h, 'create')
    assert h.response()['key'] not in caplog.text
    assert headers['Cookie'] not in caplog.text
    assert headers['X-CSRF-Token'] not in caplog.text
