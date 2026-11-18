"""
unit/proxy/test_snapshot_etag.py — snapshot etag 全字段覆盖 (C1) + 昂贵采集缓存 (C2)

C1 (CRIT): 修复前 snapshot etag 只覆盖 status+models，payload 其余字段组
   （system/history/token_stats/request_log/metrics_24h/local_models）不参与失效
   → 稳态下 etag 数小时不变 → dashboard 的 recent requests / 24h 指标 / GPU 温度
   全部冻结。修复：etag = 全 payload 字段组内容哈希（排除 ts/rev/etag 易变元数据），
   任意一组变化 → etag 变化 → dashboard 重取。TDD 锚点（review §7）：request_log
   变化 → etag 变化。

C2 (HIGH): "便宜的 304" 不便宜——每次轮询都跑 100k 样本 metrics 扫描 + 每 active
   服务健康探测（3×3s 重试），生产 32 线程池与 chat 转发共享，GPU 驱动挂起时轮询
   可占满池子。修复：_ExpensiveCache 单飞 (single-flight) TTL 缓存昂贵的
   status/metrics 采集；If-None-Match 命中且缓存新鲜时直接 304，不重跑昂贵工作；
   单飞保证慢健康探测突发不会堆叠占满池子（并发调用方拿旧值而非排队）。
"""

import sys
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[3]
# 稳定的 request_log 时间戳：模拟"无新请求"时两次轮询拿到同一行 → 内容不变 → 304。
# （真实 request_log 行携带的是请求发生时间，非抓取时间，稳态下保持不变。）
_FIXED_REQ_TS = 1700000000.0
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import inferfabric.proxy.handler as handler_module
from inferfabric.proxy.handler import ProxyHandler


# ─── 桩构造 ──────────────────────────────────────────────────


def _content_payload(**overrides) -> dict:
    """snapshot 的内容 payload（不含 ts/rev/etag）。"""
    p = {
        "status": {"gpu_mode": "shared", "active_services": ["qwen"]},
        "system": {"gpu_temp_c": 45.0, "gpu_util_pct": 12.0},
        "models": ["qwen"],
        "history": [],
        "token_stats": {},
        "request_log": [{"model": "qwen", "status": 200}],
        "metrics_24h": {"total_requests": 10},
        "local_models": {"discovered": [], "configured": ["qwen"]},
    }
    p.update(overrides)
    return p


def _make_pm(*, request_log=None, status_calls=None, metrics_calls=None,
             exp_ttl=1000.0, exp_cache=None):
    """构造 _handle_snapshot 所需的最小 pm 桩。status/get_metrics 计数以便断言
    昂贵采集是否被重跑（C2）。"""
    status_calls = status_calls if status_calls is not None else []
    metrics_calls = metrics_calls if metrics_calls is not None else []

    def status(include_device=True):
        status_calls.append(1)
        return {"gpu_mode": "shared", "active_services": ["qwen"]}

    def get_metrics(window, axis_models=None):
        # v6.0: get_metrics 契约新增 axis_models（配置驱动 x 轴），与
        # MetricsAggregator.get_metrics(window, axis_models) 一致；_compute_expensive
        # 传 axis_models= 时不触发 TypeError。
        metrics_calls.append(1)
        return {"total_requests": 10, "window": window}

    pm = SimpleNamespace()
    pm.mgr = SimpleNamespace(
        status=status,
        list_models=lambda: ["qwen"],
        state=SimpleNamespace(get_history=lambda n: []),
        _models={"qwen": object()},
        active_services={"qwen"},
    )
    pm.metrics = SimpleNamespace(get_metrics=get_metrics)
    # DeviceTelemetry T5: snapshot 组装消费 hub 设备遥测（观测缓存路径），
    # 桩用 NullTelemetry 空态（gpu_* 全 None，不触平台命令）。
    from inferfabric.platform.telemetry import NullTelemetry
    _null_tel = NullTelemetry()
    pm.telemetry = SimpleNamespace(
        device_telemetry=_null_tel,
        get_device_snapshot=lambda: _null_tel.snapshot(),
        token_collector=SimpleNamespace(_load_full_state=lambda: {}),
        query_request_log=lambda since, limit: (request_log if request_log is not None
                                                else [{"model": "qwen", "status": 200,
                                                       "tokens_in": 1, "tokens_out": 1,
                                                       "ttft_ms": 10.0, "duration_ms": 20.0,
                                                       "route": "/v1/chat", "key_name": "k",
                                                       "error": "", "timestamp": _FIXED_REQ_TS}]),
    )
    pm._snap_exp_ttl = exp_ttl
    pm._snap_exp_cache = exp_cache  # None → _handle_snapshot 惰性创建
    pm._status_calls = status_calls
    pm._metrics_calls = metrics_calls
    return pm


def _make_handler(headers=None):
    """最小 ProxyHandler 替身：记录响应码 / 头 / body，stub 掉 _system_info。"""
    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = headers or {}
    h.path = "/api/snapshot"
    h.command = "GET"
    h._sys = {"gpu_temp_c": 45.0, "gpu_util_pct": 12.0, "version": "test"}
    h._system_info = lambda *a, **k: dict(h._sys)
    h._records = []      # ("status", code)
    h._headers = []      # (k, v)
    h._bodies = []       # body bytes

    h.send_response = lambda code: h._records.append(("status", code))
    h.send_header = lambda k, v: h._headers.append((k, v))
    h.end_headers = lambda: None
    h._safe_write = lambda body: h._bodies.append(body)
    return h


def _last_status(h):
    return [c for kind, c in h._records if kind == "status"][-1]


def _last_etag(h):
    """从最近一次 200 body 里取 etag（quoted）。"""
    for body in reversed(h._bodies):
        if body:
            snap = json.loads(body.decode("utf-8"))
            if "etag" in snap:
                return snap["etag"]
    raise AssertionError("no 200 body with etag found; records=%s" % h._records)


# ═══════════════════════════════════════════════════════════════
# C1: etag 覆盖全部字段组
# ═══════════════════════════════════════════════════════════════


class TestSnapshotEtag:
    def test_request_log_change_changes_etag(self):
        """review §7 TDD 锚点：request_log 变化 → etag 变化。"""
        base = _content_payload()
        changed = _content_payload(request_log=[{"model": "qwen", "status": 500}])
        assert handler_module._snapshot_etag(base) != handler_module._snapshot_etag(changed)

    def test_system_change_changes_etag(self):
        """GPU 温度变化（C1 明确点名会被吞掉的 gpu_temp_c）→ etag 变化。"""
        base = _content_payload()
        changed = _content_payload(system={"gpu_temp_c": 51.0, "gpu_util_pct": 12.0})
        assert handler_module._snapshot_etag(base) != handler_module._snapshot_etag(changed)

    def test_metrics_change_changes_etag(self):
        base = _content_payload()
        changed = _content_payload(metrics_24h={"total_requests": 999})
        assert handler_module._snapshot_etag(base) != handler_module._snapshot_etag(changed)

    def test_status_change_changes_etag(self):
        base = _content_payload()
        changed = _content_payload(status={"gpu_mode": "exclusive", "active_services": ["llama"]})
        assert handler_module._snapshot_etag(base) != handler_module._snapshot_etag(changed)

    def test_ts_only_change_same_etag(self):
        """ts 是每次轮询都变的易变元数据，不参与 etag。"""
        base = _content_payload()
        same = dict(base)
        assert handler_module._snapshot_etag({**base, "ts": 1000}) == handler_module._snapshot_etag({**same, "ts": 2000})

    def test_rev_etag_excluded(self):
        """rev/etag 自身不影响内容哈希（避免自引用）。"""
        base = _content_payload()
        assert handler_module._snapshot_etag({**base, "rev": "abc", "etag": '"x"'}) == handler_module._snapshot_etag(base)


# ═══════════════════════════════════════════════════════════════
# C2: _ExpensiveCache 单飞 TTL
# ═══════════════════════════════════════════════════════════════


class TestExpensiveCache:
    def test_within_ttl_compute_once(self):
        calls = []

        def compute():
            calls.append(1)
            return {"status": {"v": len(calls)}}

        cache = handler_module._ExpensiveCache(ttl=1000.0)
        v0 = cache.get_or_refresh(compute)
        v1 = cache.get_or_refresh(compute)
        assert len(calls) == 1, "within TTL the expensive collect must run once"
        assert v1 == v0

    def test_after_ttl_recomputes(self, monkeypatch):
        calls = []

        def compute():
            calls.append(1)
            return {"status": {"v": len(calls)}}

        cache = handler_module._ExpensiveCache(ttl=1.0)
        cache.get_or_refresh(compute)
        real_time = handler_module.time.time
        monkeypatch.setattr(handler_module.time, "time", lambda: real_time() + 100)
        v = cache.get_or_refresh(compute)
        assert len(calls) == 2, "past TTL the expensive collect must re-run"
        assert v["status"]["v"] == 2

    def test_in_flight_returns_stale_no_double_compute(self):
        """单飞：另一线程正在重算（持有 relock）时，并发方拿旧值而非触发第二次
        慢采集（GPU 驱动挂起时健康探测 9s，堆叠会占满 32 线程池）。"""
        calls = []

        def compute():
            calls.append(1)
            return {"status": {"v": len(calls)}}

        cache = handler_module._ExpensiveCache(ttl=1000.0)
        v0 = cache.get_or_refresh(compute)
        assert len(calls) == 1

        # 强制 entry 过期 + 模拟一次重算正在进行（占住 relock）
        cache._entry = (v0, time.time() - 9999)
        cache._relock.acquire()
        try:
            v1 = cache.get_or_refresh(compute)
        finally:
            cache._relock.release()
        assert v1 == v0, "in-flight caller must use the stale value"
        assert len(calls) == 1, "must NOT double-collect while a recompute is in flight"

    def test_first_call_populates(self):
        calls = []

        def compute():
            calls.append(1)
            return {"status": {"ok": True}}

        cache = handler_module._ExpensiveCache(ttl=1000.0)
        v = cache.get_or_refresh(compute)
        assert v == {"status": {"ok": True}}
        assert len(calls) == 1


# ═══════════════════════════════════════════════════════════════
# C1+C2 集成：_handle_snapshot 304 不重跑昂贵采集
# ═══════════════════════════════════════════════════════════════


class TestHandleSnapshot304:
    def test_first_request_200_collects(self):
        pm = _make_pm()
        h = _make_handler()
        h._handle_snapshot(pm)
        assert _last_status(h) == 200
        assert len(pm._status_calls) == 1
        assert len(pm._metrics_calls) == 1

    def test_matching_imn_304_no_expensive_rerun(self):
        """C2 核心：命中 If-None-Match 且昂贵缓存新鲜 → 304，status/metrics 不重跑。"""
        pm = _make_pm()
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        etag = _last_etag(h1)

        h2 = _make_handler(headers={"If-None-Match": etag})
        h2._handle_snapshot(pm)
        assert _last_status(h2) == 304
        # 昂贵采集仍只跑过第一次（304 路径不重跑）
        assert len(pm._status_calls) == 1, "304 must NOT re-run mgr.status()"
        assert len(pm._metrics_calls) == 1, "304 must NOT re-run metrics.get_metrics()"

    def test_changed_content_returns_200(self):
        """C1：内容变化（request_log）→ etag 变 → 旧 imn 不再命中 → 200。
        昂贵采集仍在 TTL 内 → 不重跑（只重取便宜的 request_log）。"""
        pm = _make_pm()
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        etag = _last_etag(h1)

        # 让 request_log 变化（新的请求行）→ 内容 etag 变化
        pm.telemetry.query_request_log = lambda since, limit: [
            {"model": "qwen", "status": 200, "tokens_in": 5, "tokens_out": 5,
             "ttft_ms": 1.0, "duration_ms": 2.0, "route": "/v1/embed", "key_name": "k",
             "error": "", "timestamp": time.time()},
        ]
        h2 = _make_handler(headers={"If-None-Match": etag})
        h2._handle_snapshot(pm)
        assert _last_status(h2) == 200
        new_etag = _last_etag(h2)
        assert new_etag != etag, "request_log change must invalidate the etag (C1)"

    def test_stale_exp_cache_recomputes(self):
        """C2：昂贵缓存过期 → 下次 200 重跑 status/metrics。"""
        pm = _make_pm(exp_ttl=1.0, exp_cache=handler_module._ExpensiveCache(ttl=1.0))
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        assert len(pm._status_calls) == 1

        # 推进时间越过 TTL（直接改缓存 entry 的 at 字段）
        pm._snap_exp_cache._entry = (pm._snap_exp_cache._entry[0], time.time() - 9999)
        h2 = _make_handler()
        h2._handle_snapshot(pm)
        assert _last_status(h2) == 200
        assert len(pm._status_calls) == 2, "expired exp cache must re-run mgr.status()"
        assert len(pm._metrics_calls) == 2, "expired exp cache must re-run metrics.get_metrics()"
