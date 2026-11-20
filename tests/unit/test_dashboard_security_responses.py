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


class _FakeHandler:
    """只注入 _read_body/_send_json 行为（不走 socket）。"""

    def __init__(self, body):
        self._body = body
        self.replies = []

    def _read_body(self):
        return self._body

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
        assert set(k) <= {"id", "name", "created_at", "revoked"}


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
