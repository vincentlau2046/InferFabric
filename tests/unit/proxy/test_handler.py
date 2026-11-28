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


# ═══════════════════════════════════════════════════════════════
# 7. /api/request_log window 模式（dashboard-ux-v3 B1）
# ═══════════════════════════════════════════════════════════════
#
# 经真实 IFFDB + TelemetryHub 链路钉死计划 B1 契约：
# - 过滤先于 LIMIT 的同快照计数（total/failed_total 不随页大小缩小）
# - 同秒 (timestamp, id) DESC 排序，offset 翻页无重叠不漏不计重
# - [since, until) 边界；until 缺省由服务端取当前时刻且只接受非未来有限值
# - 安全池投影 {name, route_kind, target_port, retry_count, fallback_count}
#   或 null；不透出 metadata / attempts / 上游错误原文
# - 旧参数模式行为与行形状逐字不变；非法参数 400 且不回显

import math as _math
import time as _time
from types import SimpleNamespace as _NS

from inferfabric.proxy.handler import ProxyHandler as _PH

LEGACY_ROW_KEYS = {
    "timestamp", "model", "status", "tokens_in", "tokens_in_cached",
    "tokens_out", "ttft_ms", "duration_ms", "route", "key_name", "error",
    "agent", "ua",
}


def _handler(path):
    h = _PH.__new__(_PH)
    h.path = path
    h.command = "GET"
    h.headers = {}  # BaseHTTPRequestHandler 恒有
    h._sent = []
    h._send_json = lambda data, code=200: h._sent.append((code, data))
    return h


def _win_row(i, ts, model="m1", status=200, req_id=None, metadata=None,
             agent="claude-code"):
    return {
        "req_id": req_id or f"r{i}", "key_name": "k", "model": model,
        "status": status, "ttft_ms": 10.0, "tokens_in": 1,
        "tokens_in_cached": 0, "tokens_out": 2, "duration_ms": 5.0,
        "route": "local", "cloud_provider": None, "error": None,
        "timestamp": ts, "ts": str(ts), "agent": agent, "ua": "ua/1",
        "metadata": metadata if metadata is not None else {},
    }


def _seed_bulk(db):
    """>500 行混合数据：跨窗口、同秒簇、非 2xx、until 边界行。"""
    rows = []
    # m1 窗口内 550 行（6500..7049，每秒一行）：500×200 + 30×404 + 20×503
    for i in range(550):
        st = 200 if i < 500 else (404 if i < 530 else 503)
        rows.append(_win_row(i, 6500.0 + i, model="m1", status=st))
    # m2 同秒簇 60 行（7000.0）：40×200 + 10×404 + 10×503
    for i in range(60):
        st = 200 if i < 40 else (404 if i < 50 else 503)
        rows.append(_win_row(1000 + i, 7000.0, model="m2", status=st,
                             req_id=f"s{i:03d}"))
    # m3 窗口内 50 行（7100..7149）全 200——保证 m2 过滤精确
    for i in range(50):
        rows.append(_win_row(2000 + i, 7100.0 + i, model="m3"))
    # 窗口外（< since=6400）30 行，含 4xx——不得计入计数
    for i in range(30):
        rows.append(_win_row(3000 + i, 6300.0, status=404))
    # until 边界行（timestamp == until → [since, until) 排除）与未来行
    rows.append(_win_row(9001, 10000.0, model="m1", status=404))
    rows.append(_win_row(9002, 10001.0, model="m1"))
    db.insert_request_log(rows)


def _pm(tmp_path):
    import inferfabric.migrations  # noqa: F401 — 注册迁移后再建库
    from inferfabric.telemetry import TelemetryHub
    return _NS(telemetry=TelemetryHub(tmp_path))


def _window_page(pm, query):
    h = _handler(f"/api/request_log?{query}")
    h._handle_request_log(pm)
    code, data = h._sent[-1]
    return code, data


def test_request_log_filtered_page_counts(tmp_path):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    _seed_bulk(db)
    pm = _pm(tmp_path)

    code, data = _window_page(
        pm, "window=1h&until=10000.0&model=m1&limit=50")
    assert code == 200
    assert set(data) == {"logs", "count", "total", "failed_total", "until"}
    assert data["until"] == 10000.0
    assert len(data["logs"]) == 50 and data["count"] == 50
    assert data["total"] == 550          # 过滤先于 LIMIT（边界行已排除）
    assert data["failed_total"] == 50    # 30×404 + 20×503，窗口外不计

    # total 不随页大小缩小
    code2, data2 = _window_page(
        pm, "window=1h&until=10000.0&model=m1&limit=500")
    assert code2 == 200
    assert data2["total"] == 550 and len(data2["logs"]) == 500
    assert data2["failed_total"] == 50

    # 行仅新增 req_id 与 pool（不透出 metadata/id 等库内列）
    row = data["logs"][0]
    assert "req_id" in row and "pool" in row
    assert "metadata" not in row and "id" not in row


def test_request_log_same_second_offset_pagination(tmp_path):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    _seed_bulk(db)
    pm = _pm(tmp_path)

    def page(offset):
        _, d = _window_page(
            pm, f"window=1h&until=10000.0&model=m2&limit=30&offset={offset}")
        assert d["total"] == 60 and d["failed_total"] == 20
        return [r["req_id"] for r in d["logs"]]

    p1, p2 = page(0), page(30)
    assert len(p1) == 30 and len(p2) == 30
    assert not set(p1) & set(p2)                       # 页间无重叠
    assert set(p1) | set(p2) == {f"s{i:03d}" for i in range(60)}  # 不漏不计重
    assert p1 == sorted(p1, reverse=True)              # 同秒 (ts,id) DESC
    assert p1[0] > p2[0]                               # 页序连续


def test_request_log_default_until_is_server_now(tmp_path):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    db.insert_request_log([_win_row(1, _time.time() - 10.0)])
    pm = _pm(tmp_path)
    code, data = _window_page(pm, "window=1h")
    assert code == 200
    assert data["until"] <= _time.time() + 1.0
    assert data["total"] == 1


def _pool_summary(pool="web", attempts=(), **extra):
    return {"v": 1, "pool": pool, "protocol": "openai",
            "config_revision": 1, "requested_model": "qwen38",
            "first_attempted": None, "final_target": None,
            "attempt_count": len(attempts), "retry_count": 2,
            "fallback_count": 1, "outcome": "ok", "attempts": list(attempts),
            **extra}


def test_request_log_pool_projection_local_and_cloud(tmp_path):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    db.insert_request_log([
        _win_row(1, 7000.0, req_id="loc", metadata={"pool_summary": _pool_summary(
            attempts=[{"ordinal": 1, "route": "pool:web/local:qwen38",
                       "admitted": True, "status": 200,
                       "error": "UPSTREAM RAW <html>oops</html>"}])}),
        _win_row(2, 7001.0, req_id="cld", metadata={"pool_summary": _pool_summary(
            attempts=[{"ordinal": 1, "route": "pool:web/local:qwen38",
                       "admitted": True, "status": 500},
                      {"ordinal": 2, "route": "pool:web/cloud:openai/gpt-x",
                       "admitted": True, "status": 200}])}),
    ])
    pm = _pm(tmp_path)
    _, data = _window_page(pm, "window=24h&until=10000.0")
    by_req = {r["req_id"]: r for r in data["logs"]}

    loc = by_req["loc"]
    assert loc["pool"] == {"name": "web", "route_kind": "local",
                           "target_port": None,
                           "retry_count": 2, "fallback_count": 1}
    cld = by_req["cld"]
    assert cld["pool"]["route_kind"] == "cloud"   # 取最后一次 admitted
    # 上游错误原文与全量 metadata 不透出
    assert "UPSTREAM RAW" not in json.dumps(data)
    assert "metadata" not in loc and "attempts" not in json.dumps(loc)


def test_request_log_pool_projection_unreliable_variants(tmp_path):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    admitted_none = [{"ordinal": 1, "route": "pool:web/local:qwen38",
                      "admitted": False, "status": 503}]
    db.insert_request_log([
        # 损坏 metadata（非 JSON 文本）
        _win_row(1, 7000.0, req_id="corrupt", metadata="not-json{{"),
        # 旧行：空 metadata
        _win_row(2, 7001.0, req_id="old"),
        # 池名含 '/'（config 侧禁止；读侧防御 → null）
        _win_row(3, 7002.0, req_id="slash", metadata={"pool_summary": _pool_summary(
            pool="a/b")}),
        # 池名非字符串
        _win_row(4, 7003.0, req_id="nonstr", metadata={"pool_summary": _pool_summary(
            pool=123)}),
        # 缺 v（旧/损坏摘要）
        _win_row(5, 7004.0, req_id="nov", metadata={"pool_summary": {
            "pool": "web", "retry_count": 1, "fallback_count": 0}}),
        # 无 admitted attempt：route_kind 不可识别，行照常可读
        _win_row(6, 7005.0, req_id="noadm", metadata={"pool_summary": _pool_summary(
            outcome="pool_unavailable", retry_count=0, fallback_count=0,
            attempts=admitted_none)}),
    ])
    pm = _pm(tmp_path)
    _, data = _window_page(pm, "window=24h&until=10000.0")
    by_req = {r["req_id"]: r for r in data["logs"]}

    assert data["total"] == 6
    for req in ("corrupt", "old", "slash", "nonstr", "nov"):
        assert by_req[req]["pool"] is None, req
        assert by_req[req]["model"] and by_req[req]["status"]  # 行照常可读
    noadm = by_req["noadm"]
    assert noadm["pool"]["name"] == "web"
    assert noadm["pool"]["route_kind"] is None   # 无 admitted 记录可识别
    assert noadm["pool"]["retry_count"] == 0
    assert noadm["pool"]["fallback_count"] == 0


def test_request_log_legacy_mode_unchanged(tmp_path):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    db.insert_request_log([_win_row(1, 7000.0, metadata={"pool_summary": _pool_summary()})])
    pm = _pm(tmp_path)
    h = _handler("/api/request_log?since=0&limit=5")
    h._handle_request_log(pm)
    code, data = h._sent[-1]
    assert code == 200
    assert set(data) == {"logs", "count"}        # 旧响应形状不变
    assert set(data["logs"][0]) == LEGACY_ROW_KEYS  # 旧行形状不变，不加 pool


@pytest.mark.parametrize("query,secret", [
    ("window=bogus", "bogus"),
    ("window=1h&status=3xx", "3xx"),
    ("window=1h&limit=501", "501"),
    ("window=1h&limit=abc", "abc"),
    ("window=1h&offset=-1", "-1"),
    ("window=1h&offset=abc", "abc"),
    ("window=1h&until=99999999999", "99999999999"),
    ("window=1h&until=inf", "inf"),
    ("window=1h&until=nan", "nan"),
])
def test_request_log_invalid_window_params_400(tmp_path, query, secret):
    import inferfabric.migrations  # noqa: F401
    from inferfabric.db import IFFDB
    db = IFFDB(tmp_path)
    _seed_bulk(db)
    pm = _pm(tmp_path)
    h = _handler(f"/api/request_log?{query}")
    h._handle_request_log(pm)
    code, data = h._sent[-1]
    assert code == 400, (query, data)
    assert secret not in json.dumps(data)        # 不回显非法值
