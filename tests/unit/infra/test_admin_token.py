"""
unit/infra/test_admin_token.py — 控制面凭证概念删除（issue #47）契约

测试对象: inferfabric.proxy.handler._admin_gate / ProxyHandler._check_admin
新契约（2026-11-25 用户裁决：单人本机工作台零凭证）:
  - admin token 概念整体删除：born-token/严格模式/env 变量在模块上不存在
  - X-Admin-Token 头即使携带也被无视（回环放行，不比对）
  - Host/Origin 浏览器安全校验保留（跨站/DNS rebinding → 403）
"""

from unittest.mock import MagicMock

import pytest


class TestTokenConceptRemoved:
    """概念删除 pin：模块上不得再有 token 面。"""

    def test_token_symbols_absent(self):
        import inferfabric.proxy.handler as h_mod
        for gone in ("_ADMIN_TOKEN", "_ensure_admin_token", "bootstrap_admin_token",
                     "_validate_admin_token_safety", "_admin_auth_enabled"):
            assert not hasattr(h_mod, gone), f"concept must be deleted: {gone}"

    def test_gate_source_has_no_token_logic(self):
        import inspect
        import inferfabric.proxy.handler as h_mod
        source = inspect.getsource(h_mod._admin_gate)
        assert "X-Admin-Token" not in source
        assert "IFF_ADMIN" not in source
        assert "compare_digest" not in source


class TestAdminGateZeroCredential:
    """零凭证门：回环 + 任意/缺失 X-Admin-Token 一律放行。"""

    @staticmethod
    def _handler(h_mod, headers, command="GET"):
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = dict(headers)
        h.command = command
        h._send_json = MagicMock()
        return h

    def test_no_token_loopback_passes(self, monkeypatch):
        import inferfabric.proxy.handler as h_mod
        monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
        monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
        h = self._handler(h_mod, {})
        assert h._check_admin() is True
        h._send_json.assert_not_called()
        # 概念删除后不再挂 _admin_token 属性
        assert not hasattr(h, "_admin_token")

    def test_arbitrary_token_header_ignored(self, monkeypatch):
        """携带任意 X-Admin-Token（对/错/垃圾）均不影响放行 — 头已无语义。"""
        import inferfabric.proxy.handler as h_mod
        for tk in ("test-secret", "wrong-secret", "", "非ASCII值"):
            h = self._handler(h_mod, {"X-Admin-Token": tk})
            assert h._check_admin() is True, f"token={tk!r} must be ignored"
            h._send_json.assert_not_called()

    def test_strict_mode_env_is_dead(self, monkeypatch):
        """IFF_ADMIN_REQUIRED=1 不再产生任何 401 — 严格模式概念已删除。"""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
        h = self._handler(h_mod, {})
        assert h._check_admin() is True
        h._send_json.assert_not_called()


class TestBrowserSecurityRetained:
    """Host/Origin 防护保留（浏览器安全 ≠ 用户门槛）。"""

    @staticmethod
    def _handler(h_mod, headers, command="POST"):
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = dict(headers)
        h.command = command
        h._send_json = MagicMock()
        return h

    def test_untrusted_host_403(self):
        import inferfabric.proxy.handler as h_mod
        h = self._handler(h_mod, {"Host": "evil.test"})
        assert h._check_admin() is False
        args, _ = h._send_json.call_args
        assert args[1] == 403 and "untrusted host" in args[0]["error"]

    def test_cross_site_origin_403(self):
        import inferfabric.proxy.handler as h_mod
        h = self._handler(h_mod, {"Host": "127.0.0.1:8999",
                                  "Origin": "http://evil.test"})
        assert h._check_admin() is False
        args, _ = h._send_json.call_args
        assert args[1] == 403 and "untrusted origin" in args[0]["error"]

    def test_loopback_origin_passes(self):
        import inferfabric.proxy.handler as h_mod
        h = self._handler(h_mod, {"Host": "127.0.0.1:8999",
                                  "Origin": "http://127.0.0.1:8999"})
        assert h._check_admin() is True
        h._send_json.assert_not_called()
