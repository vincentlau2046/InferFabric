"""
unit/proxy/test_handler.py — ProxyHandler 核心方法单元测试

测试对象:
  - ProxyHandler._check_admin (token 验证)
  - ProxyHandler._handle_cache_toggle (缓存开关往返)
  - ProxyHandler._handle_reconcile (返回 action 列表)
  - ProxyHandler._handle_deploy (部署流程)
  - ProxyHandler._handle_gpu_clear (GPU 清理)
"""

import sys
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 添加 _deps 路径
_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════


def _make_handler(monkeypatch, headers=None, admin_token=""):
    """Create a ProxyHandler with minimal mocked state.

    issue #47：admin_token 参数保留但已无语义（凭证概念删除），
    不再改环境变量、不再 reload 模块。"""
    import inferfabric.proxy.handler as handler_module
    ProxyHandler = handler_module.ProxyHandler

    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = headers or {}
    h.path = "/"
    h.command = "GET"
    h._send_json = MagicMock()
    h._read_body = MagicMock(return_value=None)
    h._serve_dashboard = MagicMock()
    return h, handler_module


class FakeResponseCache:
    """A non-MagicMock cache object for testing toggle."""
    def __init__(self, maxsize=500):
        self.maxsize = maxsize
        self._cache = {}


# ═══════════════════════════════════════════════════════════════
# 1. Admin Auth
# ═══════════════════════════════════════════════════════════════


def test_admin_zero_credential_passes(monkeypatch):
    """Issue #47：零凭证门 — 任意/缺失 token 头不影响放行（401 不存在）。"""
    for headers in ({}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": "secret123"}):
        h, _ = _make_handler(monkeypatch, headers=headers)
        assert h._check_admin() is True, headers
        h._send_json.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 2. Cache Toggle
# ═══════════════════════════════════════════════════════════════


def test_cache_toggle_off_to_on(monkeypatch):
    """缓存关闭时切换 → 开启。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    pm = MagicMock()
    # Ensure getattr returns None for response_cache, and empty dict for _runtime_config
    pm.response_cache = None
    pm._runtime_config = {}

    h._handle_cache_toggle(pm)
    assert pm.response_cache is not None  # should be enabled now
    h._send_json.assert_called_once()
    assert h._send_json.call_args[0][0]["cache_enabled"] is True


def test_cache_toggle_on_to_off(monkeypatch):
    """缓存开启时切换 → 关闭。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    pm = MagicMock()
    pm.response_cache = FakeResponseCache(maxsize=100)

    h._handle_cache_toggle(pm)
    assert pm.response_cache is None  # should be disabled now
    h._send_json.assert_called_once()
    assert h._send_json.call_args[0][0]["cache_enabled"] is False


# ═══════════════════════════════════════════════════════════════
# 3. Reconcile
# ═══════════════════════════════════════════════════════════════


def test_reconcile_returns_json(monkeypatch):
    """reconcile 返回 JSON 结果。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    pm = MagicMock()
    pm.mgr.reconcile.return_value = {"status": "ok", "actions": ["stop:stale-vllm"]}

    h._handle_reconcile(pm)
    h._send_json.assert_called_once_with({"status": "ok", "actions": ["stop:stale-vllm"]})


def test_reconcile_no_actions_needed(monkeypatch):
    """reconcile 返回空 actions 列表。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    pm = MagicMock()
    pm.mgr.reconcile.return_value = {"status": "ok", "actions": []}

    h._handle_reconcile(pm)
    h._send_json.assert_called_once_with({"status": "ok", "actions": []})


# ═══════════════════════════════════════════════════════════════
# 4. Deploy
# ═══════════════════════════════════════════════════════════════


def test_deploy_missing_name(monkeypatch):
    """缺少 name 参数返回 400。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    h._read_body.return_value = {}
    pm = MagicMock()

    h._handle_deploy(pm)
    h._send_json.assert_called_once()
    assert "error" in h._send_json.call_args[0][0]


def test_deploy_with_name(monkeypatch):
    """带有效 name 的 deploy 调用 auto_deploy。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    h._read_body.return_value = {"name": "test-model", "type": "vllm"}
    pm = MagicMock()

    h._handle_deploy(pm)
    pm.mgr.auto_deploy.assert_called_once_with("test-model", "vllm")
    h._send_json.assert_called_once()


def test_deploy_already_configured_triggers_switch(monkeypatch):
    """已配置的 model 触发 switch。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    h._read_body.return_value = {"name": "existing-model", "type": "vllm"}
    pm = MagicMock()
    pm.mgr.auto_deploy.return_value = {"status": "already_configured"}
    pm.mgr.switch.return_value = {"status": "switched"}

    h._handle_deploy(pm)
    pm.mgr.switch.assert_called_once_with("existing-model")


# ═══════════════════════════════════════════════════════════════
# 5. GPU Clear
# ═══════════════════════════════════════════════════════════════


def test_gpu_clear_success(monkeypatch):
    """GPU 清理成功返回状态和内存信息。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    pm = MagicMock()
    pm.mgr._proc.clear_gpu_cuda_state.return_value = {
        "status": "ok", "method": "gpu-reset", "before_mb": 1024, "after_mb": 256,
    }

    h._handle_gpu_clear(pm)
    pm.mgr._proc.clear_gpu_cuda_state.assert_called_once_with(gpu_index=0, force=True)
    h._send_json.assert_called_once()
    result = h._send_json.call_args[0][0]
    assert result["status"] == "ok"
    assert result["before_mb"] == 1024
    assert result["after_mb"] == 256


def test_gpu_clear_failure(monkeypatch):
    """GPU 清理失败返回错误（500）。"""
    h, _ = _make_handler(monkeypatch, admin_token="")
    pm = MagicMock()
    pm.mgr._proc.clear_gpu_cuda_state.side_effect = Exception("nvidia-smi not found")

    h._handle_gpu_clear(pm)
    call_args = h._send_json.call_args
    assert call_args[0][0]["status"] == "error"
    assert call_args[0][1] == 500


# ═══════════════════════════════════════════════════════════════
# 6. Admin Guard Integration
# ═══════════════════════════════════════════════════════════════


def test_admin_guard_blocks_untrusted_host(monkeypatch):
    """_admin guard 阻止不可信 Host（浏览器安全保留；凭证概念已删除）。"""
    h, mod = _make_handler(monkeypatch, headers={"Host": "evil.test"})
    _admin = mod._admin
    pm = MagicMock()

    fn = MagicMock()
    guarded = _admin(fn)
    guarded(h, pm)
    # handler should NOT have been called
    fn.assert_not_called()
    # but 403 should have been sent
    h._send_json.assert_called_once()
    assert h._send_json.call_args[0][1] == 403


def test_admin_guard_allows_authorized(monkeypatch):
    """_admin guard 允许授权请求。"""
    h, mod = _make_handler(monkeypatch, headers={"X-Admin-Token": "secret123"}, admin_token="secret123")
    _admin = mod._admin
    pm = MagicMock()

    fn = MagicMock()
    guarded = _admin(fn)
    guarded(h, pm)
    fn.assert_called_once_with(h, pm)

def test_health_route_carries_instance_identity(monkeypatch):
    """issue #50：/health 暴露版本/端口/数据目录/孤儿防护状态 —
    双实例并存时一眼可辨哪个是生产。"""
    import os as _os
    from unittest.mock import MagicMock
    from inferfabric.proxy import handler as _handler
    h, _ = _make_handler(monkeypatch)
    pm = MagicMock()
    pm.mgr.gpu_mode = "idle"
    _handler._GET_ROUTES["/health"](h, pm)
    payload = h._send_json.call_args[0][0]
    assert payload["status"] == "ok"
    assert payload["gpu_mode"] == "idle"
    assert payload["version"] == _handler.__version__
    assert payload["port"] == _handler.PROXY_PORT
    assert payload["data_dir"] == str(_handler.IFF_DATA_DIR)
    assert payload["parent_watch"] == (_os.environ.get("IFF_PARENT_WATCH") == "1")
