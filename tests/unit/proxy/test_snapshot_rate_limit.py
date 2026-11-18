"""
unit/proxy/test_snapshot_rate_limit.py — R11b: snapshot local_models.rate_limit

Dashboard「网关控制」速率限制行是**只读指示器**（非开关）：徽章/副标题数据源
= snapshot local_models.rate_limit（pm.dual_gate.describe()，真实生效配置）。
契约：
  - pm.dual_gate 存在 → rate_limit = describe()（mode/server_rpm/model_rpm_default/
    max_concurrent/global_max_concurrent 全量真值）
  - pm 无 dual_gate（旧 proxy / 最小桩）→ rate_limit = None（前端回退静态标签
    「配置见 iff.yaml」，不白屏不报错）
  - rate_limit 变化参与 etag（C1 全字段覆盖：改了 iff.yaml 重启后 dashboard
    轮询必须感知到徽章/副标题变化）
"""
import sys
import json
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.handler as handler_module
from inferfabric.proxy.handler import ProxyHandler


def _make_pm(dual_gate=None, exp_ttl=1000.0):
    """_handle_snapshot 所需的最小 pm 桩（模式同 test_snapshot_etag._make_pm）。"""
    pm = SimpleNamespace()
    pm.mgr = SimpleNamespace(
        status=lambda: {"gpu_mode": "shared", "active_services": []},
        list_models=lambda: [],
        state=SimpleNamespace(get_history=lambda n: []),
        _models={},
        active_services=set(),
    )
    pm.metrics = SimpleNamespace(get_metrics=lambda window, axis_models=None: {})
    # DeviceTelemetry T5: snapshot 组装消费 hub 设备遥测；NullTelemetry 空态桩。
    from inferfabric.platform.telemetry import NullTelemetry
    _null_tel = NullTelemetry()
    pm.telemetry = SimpleNamespace(
        device_telemetry=_null_tel,
        get_device_snapshot=lambda: _null_tel.snapshot(),
        token_collector=SimpleNamespace(_load_full_state=lambda: {}),
        query_request_log=lambda since, limit: [],
    )
    pm._snap_exp_ttl = exp_ttl
    pm._snap_exp_cache = None  # None → _handle_snapshot 惰性创建
    if dual_gate is not None:
        pm.dual_gate = dual_gate
    return pm


def _make_gate(mode="observe", server_rpm=0, model_rpm_default=0,
               max_concurrent=8, global_max_concurrent=0):
    return SimpleNamespace(describe=lambda: {
        "mode": mode, "server_rpm": server_rpm,
        "model_rpm_default": model_rpm_default,
        "max_concurrent": max_concurrent,
        "global_max_concurrent": global_max_concurrent,
    })


def _call_snapshot(dual_gate=None):
    """跑一次 GET /api/snapshot，返回 (http_code, payload_dict)。"""
    pm = _make_pm(dual_gate=dual_gate)
    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = {}
    h.path = "/api/snapshot"
    h.command = "GET"
    h._sys = {"gpu_temp_c": 45.0, "gpu_util_pct": 12.0, "version": "test"}
    h._system_info = lambda *a, **k: dict(h._sys)
    records, bodies = [], []
    h.send_response = lambda code: records.append(code)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h._safe_write = lambda body: bodies.append(body)
    h._handle_snapshot(pm)
    assert records[-1] == 200, f"expected 200, got {records}"
    return json.loads(bodies[-1].decode("utf-8"))


def test_rate_limit_from_dual_gate():
    """pm.dual_gate 存在 → snapshot local_models.rate_limit = describe() 真值。"""
    snap = _call_snapshot(dual_gate=_make_gate(mode="reject", server_rpm=120,
                                               model_rpm_default=30, max_concurrent=4))
    assert snap["local_models"]["rate_limit"] == {
        "mode": "reject", "server_rpm": 120, "model_rpm_default": 30,
        "max_concurrent": 4, "global_max_concurrent": 0,
    }


def test_rate_limit_none_without_dual_gate():
    """pm 无 dual_gate → rate_limit = None（前端回退静态标签，不报错）。"""
    snap = _call_snapshot(dual_gate=None)
    assert snap["local_models"]["rate_limit"] is None


def test_rate_limit_change_changes_etag():
    """C1 全字段覆盖：rate_limit 变化 → etag 变化（dashboard 轮询感知配置变更）。"""
    a = _call_snapshot(dual_gate=_make_gate(mode="observe"))
    b = _call_snapshot(dual_gate=_make_gate(mode="reject"))
    assert a["etag"] != b["etag"]
