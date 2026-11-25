"""控制面门禁（issue #47 后契约）— 零凭证 + Host·Origin 浏览器安全。

Contract（2026-11-25 用户裁决：单人本机工作台零凭证）:
- _admin_gate 不再校验任何凭证（token 头/IFF_ADMIN_TOKEN/
  IFF_ADMIN_REQUIRED 概念整体删除；born-token 一并删除）。
- Host/Origin 校验保留：非回环 Host → 403；跨站 Origin（写）→ 403；
  本机原生应用无 Origin → 放行；IPv6 回环 bracket 形式正确解析（N1）。
- connect_handlers._gate 同样零凭证（对端/Host/Origin 三重校验保留）。
"""

import pytest

from inferfabric.proxy import handler as proxy_handler
from inferfabric.proxy import connect_handlers as connect_mod


class _FakeHandler:
    """Minimal stand-in (pure-function gate tests, no socket)."""

    def __init__(self, headers, command="POST"):
        self.headers = headers
        self.command = command
        self.replied = None

    def _send_json(self, data, status=200, extra_headers=None):
        self.replied = (data, status)


# ── 零凭证门：token 头无语义 ──

def test_admin_gate_open_without_any_header():
    h = _FakeHandler({})
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is True
    assert h.replied is None


def test_admin_gate_token_header_has_no_semantics():
    """任意 token 头（对/错/非 ASCII）一律不影响放行 — 概念已删除。"""
    for tk in ("tok-abc", "wrong", "tök-énv"):
        h = _FakeHandler({"X-Admin-Token": tk})
        assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is True, tk
        assert h.replied is None


def test_strict_mode_env_dead(monkeypatch):
    """IFF_ADMIN_REQUIRED=1 不再产生 401 — 严格模式概念删除。"""
    monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
    h = _FakeHandler({})
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is True


def test_non_loopback_bind_still_gateless(monkeypatch):
    """B1 反转：非回环 bind 也不再强制凭证（用户裁决零门槛；
    浏览器安全由 Host/Origin 承担）。"""
    monkeypatch.setattr(proxy_handler, "PROXY_HOST", "0.0.0.0")
    h = _FakeHandler({"Host": "0.0.0.0:8999"})
    assert proxy_handler._admin_gate(h, "0.0.0.0:8999") is True


# ── Host/Origin 浏览器安全（保留） ──

def test_admin_gate_rejects_non_loopback_host():
    h = _FakeHandler({"Host": "0.0.0.0:8999"})
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999") is False
    assert h.replied[1] == 403


def test_admin_gate_rejects_cross_site_origin_on_write():
    h = _FakeHandler({"Origin": "http://evil.example"})
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999", write=True) is False
    assert h.replied[1] == 403


def test_admin_gate_allows_native_app_no_origin():
    h = _FakeHandler({})
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999", write=True) is True


def test_admin_gate_allows_loopback_origin():
    h = _FakeHandler({"Origin": "http://127.0.0.1:8999"})
    assert proxy_handler._admin_gate(h, "127.0.0.1:8999", write=True) is True


def test_admin_gate_ipv6_loopback_host_accepted():
    """N1: "[::1]:8999" Host must resolve to ::1, not "[" (which 403'd)."""
    h = _FakeHandler({"Host": "[::1]:8999"})
    assert proxy_handler._admin_gate(h, "[::1]:8999") is True
    assert h.replied is None


def test_admin_gate_ipv6_origin_accepted():
    """N1: bracketed IPv6 Origin netloc resolves to ::1 (write request)."""
    h = _FakeHandler({"Origin": "http://[::1]:8999"})
    assert proxy_handler._admin_gate(h, "[::1]:8999", write=True) is True


# ── connect 门禁：零凭证，对端/Host/Origin 保留 ──

def _connect_handler(headers):
    h = _FakeHandler(headers)
    h._peer_address = "127.0.0.1"
    h.client_address = ("127.0.0.1", 0)
    return h


def test_connect_gate_open_on_loopback_without_token():
    h = _connect_handler({"Host": "127.0.0.1:8999",
                          "Origin": "http://127.0.0.1:8999"})
    assert connect_mod._gate(h, write=True) is True
    assert h.replied is None


def test_connect_gate_token_header_has_no_semantics():
    """原「无 token 一律 401」契约废除 — token 头无语义。"""
    h = _connect_handler({"Host": "127.0.0.1:8999",
                          "Origin": "http://127.0.0.1:8999",
                          "X-Admin-Token": "whatever"})
    assert connect_mod._gate(h, write=True) is True


def test_connect_gate_rejects_cross_site_origin():
    h = _connect_handler({"Host": "127.0.0.1:8999",
                          "Origin": "http://evil.example"})
    assert connect_mod._gate(h, write=True) is False
    assert h.replied[1] == 403


def test_connect_gate_rejects_non_loopback_peer():
    h = _FakeHandler({"Host": "127.0.0.1:8999",
                      "Origin": "http://127.0.0.1:8999"})
    h.client_address = ("192.168.1.9", 0)
    assert connect_mod._gate(h, write=True) is False
    assert h.replied[1] == 403
