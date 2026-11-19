"""
unit/infra/test_admin_token.py — Admin Token 安全测试

测试对象: inferfabric.proxy.handler.ProxyHandler._check_admin, _validate_admin_token_safety
覆盖范围:
  - hmac.compare_digest 恒定时间比较（源码验证）
  - 空 token + 非 localhost → RuntimeError（fail-fast）
  - 空 token + localhost → 放行
  - 正确/错误 token 集成测试
"""

import os
import hmac
import importlib
from unittest.mock import MagicMock, patch

import pytest


class TestCheckAdminConstantTime:
    """Verify the admin gate uses hmac.compare_digest.

    F7: _check_admin delegates to _admin_gate, which owns token comparison.
    """

    def test_compare_digest_usage(self):
        """Source code must use hmac.compare_digest, not ==."""
        import inspect
        import inferfabric.proxy.handler as h_mod
        source = inspect.getsource(h_mod._admin_gate)
        assert "hmac.compare_digest" in source, \
            "_admin_gate must use hmac.compare_digest for constant-time comparison"
        # Should NOT use bare == for token comparison
        stripped = source.replace(" ", "")
        assert 'token==_ADMIN_TOKEN' not in stripped and \
               'token=="_ADMIN_TOKEN"' not in source, \
            "_admin_gate should not use == for token comparison"


class TestValidateAdminTokenSafety:
    """Verify _validate_admin_token_safety enforces fail-fast.

    NOTE: PROXY_HOST and _ADMIN_TOKEN are module-level constants in handler.py,
    read from os.environ at import time. monkeypatch.setenv alone won't work
    because the module is already cached in sys.modules. We must use
    monkeypatch.setattr on the module attributes directly.
    """

    def test_empty_token_non_localhost_raises(self, monkeypatch):
        """Empty token + non-localhost PROXY_HOST → RuntimeError."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "0.0.0.0")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        with pytest.raises(RuntimeError) as excinfo:
            h_mod._validate_admin_token_safety()
        assert "not localhost" in str(excinfo.value).lower() or \
               "IFF_ADMIN_TOKEN" in str(excinfo.value)

    def test_empty_token_localhost_ok(self, monkeypatch):
        """Empty token + localhost PROXY_HOST → no error, just warning."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "127.0.0.1")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h_mod._validate_admin_token_safety()  # should not raise

    def test_empty_token_localhost_ipv6_ok(self, monkeypatch):
        """Empty token + ::1 PROXY_HOST → no error."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "::1")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h_mod._validate_admin_token_safety()

    def test_empty_token_localhost_name_ok(self, monkeypatch):
        """Empty token + 'localhost' PROXY_HOST → no error."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "localhost")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "")
        h_mod._validate_admin_token_safety()

    def test_token_set_any_host_ok(self, monkeypatch):
        """Token set + any PROXY_HOST → no error."""
        import inferfabric.proxy.handler as h_mod
        monkeypatch.setattr(h_mod, "PROXY_HOST", "0.0.0.0")
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", "secret123")
        h_mod._validate_admin_token_safety()


class TestAdminTokenIntegration:
    """Integration tests for _check_admin in ProxyHandler.

    F7 contract: token enforcement is strict-mode-only (IFF_ADMIN_REQUIRED=1);
    default mode stays open on loopback (zero-friction install). Wrong/missing
    token therefore require strict mode to yield 401.
    """

    @staticmethod
    def _handler(monkeypatch, h_mod, admin_token, headers, command="GET", strict=False):
        if strict:
            monkeypatch.setenv("IFF_ADMIN_REQUIRED", "1")
        else:
            monkeypatch.delenv("IFF_ADMIN_REQUIRED", raising=False)
            monkeypatch.delenv("MACFABRIC_ADMIN_REQUIRED", raising=False)
        monkeypatch.setattr(h_mod, "_ADMIN_TOKEN", admin_token)
        h = h_mod.ProxyHandler.__new__(h_mod.ProxyHandler)
        h.headers = dict(headers)
        h.command = command
        h._send_json = MagicMock()
        return h

    def test_correct_token_accepted(self, monkeypatch):
        """Correct token passes check."""
        import inferfabric.proxy.handler as h_mod
        h = self._handler(monkeypatch, h_mod, "test-secret",
                          {"X-Admin-Token": "test-secret"})
        result = h._check_admin()
        assert result is True
        h._send_json.assert_not_called()

    def test_wrong_token_rejected(self, monkeypatch):
        """Wrong token fails check with 401 (strict mode)."""
        import inferfabric.proxy.handler as h_mod
        h = self._handler(monkeypatch, h_mod, "test-secret",
                          {"X-Admin-Token": "wrong-secret"}, strict=True)
        result = h._check_admin()
        assert result is False
        h._send_json.assert_called_once()
        args, _ = h._send_json.call_args
        assert args[1] == 401

    def test_empty_token_config_allows_all(self, monkeypatch):
        """When _ADMIN_TOKEN is empty and strict mode is off, requests pass."""
        import inferfabric.proxy.handler as h_mod
        h = self._handler(monkeypatch, h_mod, "", {})
        result = h._check_admin()
        assert result is True
        h._send_json.assert_not_called()

    def test_empty_header_rejected(self, monkeypatch):
        """Missing X-Admin-Token header is rejected when token is set (strict mode)."""
        import inferfabric.proxy.handler as h_mod
        h = self._handler(monkeypatch, h_mod, "secret", {}, strict=True)
        result = h._check_admin()
        assert result is False
