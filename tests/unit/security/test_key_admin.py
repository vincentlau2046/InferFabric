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
    h = Handler({'name': 'client', 'models': ['fabric-work']}, headers)
    admin.handle(h, 'create')
    assert h.status == 201
    token, meta = h.response()['key'], h.response()['entry']
    assert admin.store.check(token, 'fabric-work')[0]
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
    assert h.status == 200 and not admin.store.check(token, 'fabric-work')[0]


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


def test_password_change_requires_session(admin):
    # PR3（WS4）：改密走控制台会话 + CSRF — 无会话 401
    h = Handler({'current_password': 'test-password-long',
                 'new_password': 'a-new-password-long'})
    admin.handle(h, 'password')
    assert h.status == 401


def test_password_change_wrong_current_rejected_with_recovery_hint(admin):
    # 逐次验密：错旧密码 → 400 + 可操作指引（ISSUE-04：健康库走 set-password）
    headers = login(admin)
    h = Handler({'current_password': 'wrong-password-x',
                 'new_password': 'a-new-password-long'}, headers)
    admin.handle(h, 'password')
    assert h.status == 400
    assert '当前密码' in h.response()['error']
    assert 'iff keys set-password' in h.response()['error']


def test_password_change_rejects_short_new(admin):
    headers = login(admin)
    hk = Handler({'name': 'client'}, headers)
    admin.handle(hk, 'create')
    key_token = hk.response()['key']
    h = Handler({'current_password': 'test-password-long',
                 'new_password': 'short'}, headers)
    admin.handle(h, 'password')
    assert h.status == 400
    assert admin.store.check(key_token, 'fabric-work')[0] is True  # 拒绝不伤既有 key


def test_password_change_success_invalidates_old_session_keeps_keys(admin):
    # 成功改密：200；旧会话随口令记录更新失效；新密码可登录；key 与策略原样
    headers = login(admin)
    hk = Handler({'name': 'client'}, headers)
    admin.handle(hk, 'create')
    key_token = hk.response()['key']

    h = Handler({'current_password': 'test-password-long',
                 'new_password': 'a-new-password-long'}, headers)
    admin.handle(h, 'password')
    assert h.status == 200

    h2 = Handler(headers=headers, method='GET')
    admin.handle(h2, 'list')
    assert h2.status == 401                      # 旧会话失效

    h3 = Handler({'password': 'a-new-password-long'})
    admin.handle(h3, 'login')
    assert h3.status == 200
    fresh = {'Cookie': h3.response_headers['Set-Cookie'].split(';')[0],
             'X-CSRF-Token': h3.response()['csrf']}
    h4 = Handler(headers=fresh, method='GET')
    admin.handle(h4, 'list')
    assert h4.status == 200
    assert admin.store.check(key_token, 'fabric-work')[0] is True  # key 仍可校验
    assert any(k.get('name') == 'client' for k in h4.response()['keys'])
