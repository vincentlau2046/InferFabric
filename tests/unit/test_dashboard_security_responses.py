"""M2-T2 — security 明文响应 no-store 契约（计划 2026-11-19 Task 2）。

两条可能返回明文的既有路由（/admin/security/enable、/admin/security/keys）
的成功响应必须携带 `Cache-Control: no-store`（Review Focus 2：明文不进
任何共享缓存）。隔离临时密钥库（tmp_path 派生 IFF_DATA_DIR），不触生产
~/.inferfabric。status/列表响应不含明文 token；库内只存摘要。
"""
import json
from types import SimpleNamespace

import pytest

from inferfabric.proxy import handler as proxy_handler
from inferfabric.proxy.auth import AuthManager
from inferfabric.security.auth_facade import build_auth
from inferfabric.security.console_auth import password_set


class _FakeHandler:
    """只注入 _read_body/_send_json 行为（不走 socket）。"""

    def __init__(self, body):
        self._body = body
        self.replies = []
        self.command = "POST"
        self.headers = {"Host": "127.0.0.1:8999",
                        "Origin": "http://127.0.0.1:8999"}

    def _read_body(self):
        return self._body

    def _check_admin(self):
        # 路由级 round-trip 测试直连 _admin 包装器：门在本套件恒放行
        # （token 门语义已由 _TokenHandler 用例单独覆盖）
        return True

    def _send_json(self, data, status=200, extra_headers=None):
        self.replies.append((data, status, dict(extra_headers or {})))

    @property
    def last(self):
        assert self.replies, "handler did not reply"
        return self.replies[-1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_handler, "IFF_DATA_DIR", tmp_path)
    pm = SimpleNamespace(auth=AuthManager(tmp_path / "api_keys.yaml"),
                         security_admin=None)
    return SimpleNamespace(tmp=tmp_path, pm=pm)


def _db_bytes(tmp):
    db = tmp / "security.db"
    return db.read_bytes() if db.exists() else b""


# ── 首次启用（自动生成首个 key，明文仅本次回显） ─────────────────

def test_first_enable_success_is_no_store(env):
    h = _FakeHandler({"enforce": True, "key_name": "qa"})
    proxy_handler.ProxyHandler._handle_security_enable(h, env.pm)
    data, status, headers = h.last
    assert status == 200 and data["status"] == "ok"
    assert headers.get("Cache-Control") == "no-store"
    token = data.get("key")
    assert isinstance(token, str) and token, "first enable must mint one key"
    assert data["policy"] == "all" and data["enforced"] is True
    # 库内只存摘要：明文不出现在 security.db 字节里
    assert token.encode() not in _db_bytes(env.tmp)
    assert token not in json.dumps(data["note"] or "")


def test_enable_off_is_no_store_without_key(env):
    proxy_handler.ProxyHandler._handle_security_enable(
        _FakeHandler({"enforce": True, "key_name": "qa"}), env.pm)
    h = _FakeHandler({"enforce": False})
    proxy_handler.ProxyHandler._handle_security_enable(h, env.pm)
    data, status, headers = h.last
    assert status == 200 and data["policy"] == "off"
    assert headers.get("Cache-Control") == "no-store"
    assert "key" not in data


# ── 主动创建 key（明文仅本次回显） ───────────────────────────────

def test_create_key_success_is_no_store(env):
    proxy_handler.ProxyHandler._handle_security_enable(
        _FakeHandler({"enforce": True, "key_name": "qa"}), env.pm)
    h = _FakeHandler({"name": "ci"})
    proxy_handler.ProxyHandler._handle_security_create_key(h, env.pm)
    data, status, headers = h.last
    assert status == 200 and data["status"] == "ok"
    assert headers.get("Cache-Control") == "no-store"
    token = data["key"]
    assert isinstance(token, str) and token
    assert token.encode() not in _db_bytes(env.tmp)


def test_create_key_requires_security(env):
    h = _FakeHandler({"name": "x"})
    proxy_handler.ProxyHandler._handle_security_create_key(h, env.pm)
    data, status, _ = h.last
    assert status == 400
    assert "key" not in data


def test_create_key_duplicate_name_is_client_error_not_connection_drop(env):
    """重名（含已吊销行）→ 400 带指引，而非未捕获 IntegrityError 沿
    do_POST 吞掉（连接无响应关闭，浏览器表现为请求挂起）。"""
    proxy_handler.ProxyHandler._handle_security_enable(
        _FakeHandler({"enforce": True, "key_name": "dup"}), env.pm)
    h = _FakeHandler({"name": "dup"})
    proxy_handler.ProxyHandler._handle_security_create_key(h, env.pm)
    data, status, _ = h.last
    assert status == 400
    assert "key" not in data
    assert "dup" in data.get("error", "")


# ── status / 吊销 ────────────────────────────────────────────────

def test_status_never_contains_plaintext(env):
    h0 = _FakeHandler({"enforce": True, "key_name": "qa"})
    proxy_handler.ProxyHandler._handle_security_enable(h0, env.pm)
    token = h0.last[0]["key"]
    proxy_handler.ProxyHandler._handle_security_create_key(
        _FakeHandler({"name": "second"}), env.pm)

    h = _FakeHandler(None)
    proxy_handler.ProxyHandler._handle_security_status(h, env.pm)
    data, status, _ = h.last
    assert status == 200
    assert data["active"] is True and data["policy"] == "all"
    assert data["key_count"] == 2
    assert token not in json.dumps(data), "plaintext token leaked in status"
    for k in data["keys"]:
        # issue #48：吊销即硬删 — 列表只含有效 key，无 revoked 字段
        assert set(k) == {"id", "name", "created_at"}
    assert data["key_count"] == len(data["keys"])


def test_revoke_drops_key_count(env):
    proxy_handler.ProxyHandler._handle_security_enable(
        _FakeHandler({"enforce": True, "key_name": "qa"}), env.pm)
    h = _FakeHandler({"name": "tmp"})
    proxy_handler.ProxyHandler._handle_security_create_key(h, env.pm)
    kid = env.pm.auth.store.list_keys()[-1]["id"]

    h = _FakeHandler({"id": kid})
    proxy_handler.ProxyHandler._handle_security_revoke_key(h, env.pm)
    data, status, _ = h.last
    assert status == 200 and data["status"] == "ok"

    h = _FakeHandler(None)
    proxy_handler.ProxyHandler._handle_security_status(h, env.pm)
    assert h.last[0]["key_count"] == 1


# ── status password_set 三态（PR3 WS4：设置页 Key 保护密码卡） ────

def test_status_reports_password_set_field(env):
    from inferfabric.security.console_auth import set_password

    # 未初始化库（legacy auth）→ 恒 False，且不得 500
    h = _FakeHandler(None)
    proxy_handler.ProxyHandler._handle_security_status(h, env.pm)
    data, status, _ = h.last
    assert status == 200 and data["password_set"] is False

    # 已初始化库、无口令（enable 不建口令）→ False
    proxy_handler.ProxyHandler._handle_security_enable(
        _FakeHandler({"enforce": True, "key_name": "qa"}), env.pm)
    h = _FakeHandler(None)
    proxy_handler.ProxyHandler._handle_security_status(h, env.pm)
    data, status, _ = h.last
    assert status == 200 and data["active"] is True and data["password_set"] is False

    # 已设口令 → True
    set_password(env.pm.auth.store, "a-console-password")
    h = _FakeHandler(None)
    proxy_handler.ProxyHandler._handle_security_status(h, env.pm)
    data, status, _ = h.last
    assert status == 200 and data["password_set"] is True


# ── 首次网页设密（PR3 WS4；issue #47 后零凭证 — 401 路径不存在） ──

class _TokenHandler(_FakeHandler):
    """带请求头的 fake。issue #47 后 X-Admin-Token 无语义（服务端不读）—
    保留传参以钉住「携带任意凭证头不影响放行」。"""

    def __init__(self, body, token=None, headers=None):
        super().__init__(body)
        self.headers = {"Host": "127.0.0.1:8999", **(headers or {})}
        if token:
            self.headers["X-Admin-Token"] = token


_TOKEN_OK = {"X-Admin-Token": "secret-token"}


def test_console_password_no_disk_on_untrusted_origin(env):
    """拒绝先于落盘（浏览器安全面，走真实路由+真实门）：跨站 Origin → 403，
    不建任何文件。"""
    h = _TokenHandler({"new_password": "a-console-password"}, token="secret-token",
                      headers={"Origin": "http://evil.test"})
    # 真实 _check_admin（而非 passthrough）驱动 Host/Origin 拒绝
    h._check_admin = lambda: proxy_handler.ProxyHandler._check_admin(h)
    proxy_handler._POST_ROUTES["/admin/security/console-password"](h, env.pm)
    data, status, _ = h.last
    assert status == 403
    assert not (env.tmp / "security.db").exists()
    assert not (env.tmp / "security.enabled").exists()


def test_console_password_first_set_provisions_store_without_policy_or_keys(env):
    from inferfabric.security.auth_facade import SecurityAuth
    h = _TokenHandler({"new_password": "a-console-password"}, token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    data, status, headers = h.last
    assert status == 200 and data == {"status": "ok", "provisioned": True}
    assert headers.get("Cache-Control") == "no-store"
    assert (env.tmp / "security.db").exists()
    assert (env.tmp / "security.enabled").exists()
    assert isinstance(env.pm.auth, SecurityAuth)
    assert env.pm.security_admin is not None
    assert env.pm.auth.store.get_policy() == "off"   # 不置 policy — enable 是用户的独立动作
    assert env.pm.auth.store.list_keys() == []       # 不建 key
    assert password_set(env.pm.auth.store) is True
    assert b"a-console-password" not in (env.tmp / "security.db").read_bytes()


def test_console_password_second_set_conflicts(env):
    proxy_handler.ProxyHandler._handle_security_console_password(
        _TokenHandler({"new_password": "a-console-password"}, token="secret-token",
                      headers=_TOKEN_OK), env.pm)
    h = _TokenHandler({"new_password": "another-long-password"}, token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    data, status, _ = h.last
    assert status == 409 and "已设置" in data["error"]


def test_console_password_rejects_short(env):
    h = _TokenHandler({"new_password": "short"}, token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    data, status, _ = h.last
    assert status == 400
    assert not (env.tmp / "security.db").exists()   # 口令校验先于任何落盘


def test_console_password_existing_store_without_password(env):
    proxy_handler.ProxyHandler._handle_security_enable(
        _FakeHandler({"enforce": True, "key_name": "qa"}), env.pm)
    policy_before = env.pm.auth.store.get_policy()
    h = _TokenHandler({"new_password": "a-console-password"}, token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    data, status, _ = h.last
    assert status == 200 and data["provisioned"] is False
    assert password_set(env.pm.auth.store) is True
    assert env.pm.auth.store.get_policy() == policy_before   # enable 的 policy 不被动
    assert env.pm.security_admin is not None


# ── 评审修复（fresh-context 2026-11-25）──────────────────────────
# C1: /admin/keys/password 必须在真实路由表可达（改密第二跳 404）
# C2: 首设热接保留在役 legacy manager（enforced 态 + reloader 身份）
# I5: origins 校验先于任何落盘（与启动同源，含 [::1]）
# I9: 非对象 JSON 体 → 400（而非无响应/500）

def test_password_route_registered_and_dispatches(env):
    """改密两跳必须走真实 _POST_ROUTES（review C1）：login → password。"""
    import io
    from inferfabric.security.admin import SecurityAdmin
    from inferfabric.security.console_auth import set_password as _sp
    from inferfabric.security.key_store import KeyStore

    class _RouteHandler:
        def __init__(self, body=None, headers=None, method="POST"):
            raw = json.dumps(body or {}).encode()
            self.headers = {"Host": "127.0.0.1:8999",
                            "Origin": "http://127.0.0.1:8999",
                            "Content-Length": str(len(raw)), **(headers or {})}
            self.command = method
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            self.response_headers = {}
            self.status = None
        def send_response(self, s): self.status = s
        def send_header(self, k, v): self.response_headers[k] = v
        def end_headers(self): pass
        def _check_admin(self): return True   # 门语义由 _TokenHandler 用例覆盖

    store = KeyStore(env.tmp / "security.db", provision=True)
    _sp(store, "test-password-long")
    env.pm.auth = build_auth(env.tmp)
    env.pm.security_admin = SecurityAdmin(store, origins=["http://127.0.0.1:8999"])

    login_route = proxy_handler._POST_ROUTES["/admin/keys/login"]
    h = _RouteHandler({"password": "test-password-long"})
    login_route(h, env.pm)
    assert h.status == 200
    cookie = h.response_headers["Set-Cookie"].split(";")[0]
    csrf = json.loads(h.wfile.getvalue())["csrf"]

    pw_route = proxy_handler._POST_ROUTES["/admin/keys/password"]
    h2 = _RouteHandler({"current_password": "test-password-long",
                        "new_password": "a-new-password-long"},
                       headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    pw_route(h2, env.pm)
    assert h2.status == 200, json.loads(h2.wfile.getvalue())


def _legacy_enforcing(tmp):
    yaml = tmp / "api_keys.yaml"
    yaml.write_text('primary: "sk-iff-test-primary-key"\n', encoding="utf-8")
    return AuthManager(yaml)


def test_first_set_preserves_enforcing_legacy_manager(env):
    """review C2：热接不得新建 legacy manager — enforced 态与 reloader 身份保留。"""
    old = _legacy_enforcing(env.tmp)
    assert old.enabled is True
    env.pm.auth = old
    h = _TokenHandler({"new_password": "a-console-password"}, token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    assert h.last[1] == 200
    from inferfabric.security.auth_facade import SecurityAuth as SA
    assert isinstance(env.pm.auth, SA)
    assert env.pm.auth.legacy is old          # 同一对象：reloader reload 生效
    assert env.pm.auth.legacy.enabled is True  # 在役强制态不被静默降级


def test_first_set_invalid_origins_rejected_before_disk(env, monkeypatch):
    """review I5：非回环 bind + 无 HTTPS origins → 400，先于任何落盘。"""
    from inferfabric import proxy_manager
    monkeypatch.setattr(proxy_manager, "PROXY_HOST", "0.0.0.0")
    monkeypatch.delenv("IFF_CONSOLE_ORIGINS", raising=False)
    h = _TokenHandler({"new_password": "a-console-password"}, token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    assert h.last[1] == 400
    assert not (env.tmp / "security.db").exists()
    assert not (env.tmp / "security.enabled").exists()


def test_first_set_non_object_body_rejected(env):
    """review I9：JSON 数组体 → 400（线程模式不得无响应关闭）。"""
    h = _TokenHandler(["not", "an", "object"], token="secret-token",
                      headers=_TOKEN_OK)
    proxy_handler.ProxyHandler._handle_security_console_password(h, env.pm)
    assert h.last[1] == 400
    assert not (env.tmp / "security.db").exists()


def test_read_body_json_null_replies_400():
    """review I9：JSON null 此前静默关闭连接 — read_body 统一 400 后保持
    「None = 已回复」契约（端点的 if data is None: return 不双写）。"""
    import io
    from inferfabric import forwarder

    class RawHandler:
        def __init__(self, raw):
            self.headers = {"Content-Length": str(len(raw))}
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            self.status = None
        def send_response(self, s): self.status = s
        def send_header(self, k, v): pass
        def end_headers(self): pass

    h = RawHandler(b"null")
    assert forwarder.read_body(h) is None
    assert h.status == 400
    h2 = RawHandler(b"{}")
    assert forwarder.read_body(h2) == {}   # 空对象不受影响


def test_async_server_has_no_token_bootstrap():
    """issue #47：async 启动不再有任何 token 引导（概念删除 pin）。"""
    import inspect
    from inferfabric.proxy import async_server
    source = inspect.getsource(async_server)
    assert "bootstrap_admin_token" not in source
    assert "_validate_admin_token_safety" not in source
    assert not hasattr(proxy_handler, "bootstrap_admin_token")
    assert not hasattr(proxy_handler, "_ADMIN_TOKEN")
