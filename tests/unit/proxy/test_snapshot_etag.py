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


def _make_real_handler(headers=None):
    """同 _make_handler，但不桩 _system_info —— 用真实 /proc 读取路径
    （7.12.20 ETag 假绿回归：uptime 每秒 +1 / cpu 小数抖动不得破坏 304）。"""
    h = ProxyHandler.__new__(ProxyHandler)
    h.headers = headers or {}
    h.path = "/api/snapshot"
    h.command = "GET"
    h._records = []
    h._headers = []
    h._bodies = []
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
# C 期：device ETag 语义钉住 + X-IFF-Device-Sample-Age
# ═══════════════════════════════════════════════════════════════


class _FakeDeviceTel:
    """可控 snapshot/capabilities 的设备遥测桩（钉住 device 值/能力/时间 ETag 语义）。"""

    def __init__(self, ts=None, used=None, caps=None):
        from inferfabric.platform.telemetry import DeviceCapabilities
        self.ts = ts if ts is not None else time.time()
        self.used = used
        self.caps = caps or DeviceCapabilities(memory=True, pressure=True)

    def snapshot(self):
        from inferfabric.platform.telemetry import DeviceSnapshot
        return DeviceSnapshot(
            platform="darwin", accelerator="apple_silicon", memory_kind="unified",
            memory_used_mb=self.used, memory_total_mb=32768, memory_budget_mb=24576,
            utilization_pct=None, power_w=None, temperature_c=None, clock_mhz=None,
            pressure_level=1, timestamp=self.ts, sources={},
        )

    def capabilities(self):
        return self.caps


def _make_pm_with_device(tel):
    pm = _make_pm()
    pm.telemetry.device_telemetry = tel
    pm.telemetry.get_device_snapshot = tel.snapshot
    return pm


def _hdr(h, name):
    """取最近一次响应中指定头的值；不存在返回 None。"""
    vals = [v for k, v in h._headers if k.lower() == name.lower()]
    return vals[-1] if vals else None


class TestDeviceEtagPinning:
    """契约 §C1：device 值/能力变化 → ETag 失效；仅采样时间变化 → 可 304。"""

    def test_device_value_change_changes_etag(self):
        tel = _FakeDeviceTel(used=1000)
        pm = _make_pm_with_device(tel)
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        etag1 = _last_etag(h1)

        tel.used = 2000
        h2 = _make_handler()
        h2._handle_snapshot(pm)
        assert _last_etag(h2) != etag1, "device 值变化必须失效 ETag"

    def test_device_capability_change_changes_etag(self):
        from inferfabric.platform.telemetry import DeviceCapabilities
        tel = _FakeDeviceTel(caps=DeviceCapabilities(memory=True))
        pm = _make_pm_with_device(tel)
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        etag1 = _last_etag(h1)

        tel.caps = DeviceCapabilities(memory=True, pressure=True)
        h2 = _make_handler()
        h2._handle_snapshot(pm)
        assert _last_etag(h2) != etag1, "能力变化必须失效 ETag"

    def test_device_timestamp_only_change_same_etag(self):
        """T5 语义：仅采样时间变化（值/来源/能力全同）→ 同 ETag → 304 可命中。"""
        tel = _FakeDeviceTel(used=1000, ts=1000.0)
        pm = _make_pm_with_device(tel)
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        etag1 = _last_etag(h1)

        tel.ts = 2000.0
        h2 = _make_handler(headers={"If-None-Match": etag1})
        h2._handle_snapshot(pm)
        assert _last_status(h2) == 304, "仅采样时间变化必须仍可 304"

    # ── 7.12.20 F-A：system 纯钟/负载字段不得让 304 永不命中 ──

    def test_system_uptime_tick_same_etag(self):
        """uptime_seconds 每秒 +1（纯钟字段、dashboard 不渲染）→ 同 ETag。"""
        base = {"gpu_temp_c": 45.0, "uptime_seconds": 214102, "cpu_percent": 70.5}
        e1 = handler_module._snapshot_etag(_content_payload(system=base))
        e2 = handler_module._snapshot_etag(
            _content_payload(system={**base, "uptime_seconds": 214105}))
        assert e1 == e2, "uptime 走表不得改变 ETag（否则 3s 轮询 304 永不命中）"

    def test_system_cpu_subinteger_jitter_same_etag(self):
        """cpu_percent 0.1 位抖动 → 同 ETag（哈希按整数量化）；
        整数位变化 → 失效（真实内容变化仍须重取）。"""
        base = {"gpu_temp_c": 45.0, "cpu_percent": 70.51}
        e1 = handler_module._snapshot_etag(_content_payload(system=base))
        e2 = handler_module._snapshot_etag(
            _content_payload(system={**base, "cpu_percent": 70.6}))
        assert e1 == e2
        e3 = handler_module._snapshot_etag(
            _content_payload(system={**base, "cpu_percent": 72.1}))
        assert e3 != e1, "整数位变化属真实内容变化，必须失效 ETag"

    def test_system_ram_subgb_jitter_same_etag(self):
        """ram_used_gb 0.1GB 分辨率在活跃机常跨格 → 哈希按整数量化；
        跨整 GB 仍失效。"""
        base = {"gpu_temp_c": 45.0, "ram_used_gb": 96.1}
        e1 = handler_module._snapshot_etag(_content_payload(system=base))
        e2 = handler_module._snapshot_etag(
            _content_payload(system={**base, "ram_used_gb": 96.4}))
        assert e1 == e2
        e3 = handler_module._snapshot_etag(
            _content_payload(system={**base, "ram_used_gb": 101.2}))
        assert e3 != e1

    def test_system_gpu_values_still_invalidate(self):
        """回归护栏：system 的 GPU 真值（rail 真显示）必须继续参与哈希。"""
        base = {"gpu_temp_c": 45.0, "uptime_seconds": 100, "cpu_percent": 1.0}
        e1 = handler_module._snapshot_etag(_content_payload(system=base))
        e2 = handler_module._snapshot_etag(
            _content_payload(system={**base, "gpu_temp_c": 46.0, "uptime_seconds": 101}))
        assert e2 != e1

    def test_real_system_info_uptime_tick_still_304(self, monkeypatch):
        """集成：不桩 _system_info，真实两轮轮询（间隔 >1s，uptime 必然前进）
        必须命中 304 —— F-A 的端到端回归（Mac 上 /proc 不可读时字段恒定，
        同样成立）。"""
        # 只冻结 loadavg/meminfo 两个仪表源（消除并行负载噪声）；uptime
        # 保持真实读取 —— 本用例要证明的正是「uptime 走表不影响 304」。
        import builtins, io
        real_open = builtins.open
        def _fake_open(path, *a, **k):
            if path == "/proc/loadavg":
                return io.StringIO("0.00 0.00 0.00 1/1 1\n")
            if path == "/proc/meminfo":
                return io.StringIO("MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
            return real_open(path, *a, **k)
        pm = _make_pm()
        h1 = _make_real_handler()
        monkeypatch.setattr(builtins, "open", _fake_open)
        try:
            h1._handle_snapshot(pm)
            assert _last_status(h1) == 200
            etag = _last_etag(h1)
            time.sleep(1.05)   # uptime 至少 +1
            h2 = _make_real_handler(headers={"If-None-Match": etag})
            h2._handle_snapshot(pm)
        finally:
            monkeypatch.setattr(builtins, "open", real_open)
        assert _last_status(h2) == 304, \
            "真实 _system_info 下 uptime 走表不得破坏 304（F-A 回归）"


class TestSampleAgeHeader:
    """契约 §C2：200/304 均带 X-IFF-Device-Sample-Age（非负整数秒）；
    无可信时间戳 → 头省略（前端标「新鲜度未确认」）。"""

    HEADER = "X-IFF-Device-Sample-Age"

    def test_present_on_200_and_matches_sample_age(self):
        tel = _FakeDeviceTel(ts=time.time() - 42.0)
        pm = _make_pm_with_device(tel)
        h = _make_handler()
        h._handle_snapshot(pm)
        assert _last_status(h) == 200
        age = _hdr(h, self.HEADER)
        assert age is not None, "200 必须带采样年龄头"
        assert 42 <= int(age) <= 43, f"年龄应为 ~42s, got {age}"

    def test_present_on_304_and_monotonic(self):
        tel = _FakeDeviceTel(ts=time.time() - 10.0)
        pm = _make_pm_with_device(tel)
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        etag = _last_etag(h1)
        age1 = int(_hdr(h1, self.HEADER))

        tel.ts = time.time() - 70.0  # 同源缓存继续被消费，采样在变老
        h2 = _make_handler(headers={"If-None-Match": etag})
        h2._handle_snapshot(pm)
        assert _last_status(h2) == 304
        age2 = _hdr(h2, self.HEADER)
        assert age2 is not None, "304 也必须带采样年龄头（不假定 304 即新采样）"
        assert int(age2) >= age1, "304 上的年龄不得早于 200 时（同一份采样在变老）"

    def test_future_timestamp_clamped_non_negative(self):
        tel = _FakeDeviceTel(ts=time.time() + 500.0)  # 时钟偏斜
        pm = _make_pm_with_device(tel)
        h = _make_handler()
        h._handle_snapshot(pm)
        age = _hdr(h, self.HEADER)
        assert age is not None
        assert int(age) == 0, "未来时间戳必须截断为 0（非负整数秒）"

    def test_absent_without_trustworthy_timestamp(self):
        from inferfabric.platform.telemetry import DeviceSnapshot
        tel = _FakeDeviceTel()
        # timestamp=None（数据契约外但防御）：省略头而非伪造 0
        broken = DeviceSnapshot(
            platform="darwin", accelerator="apple_silicon", memory_kind="unified",
            memory_used_mb=1, memory_total_mb=2, memory_budget_mb=1,
            utilization_pct=None, power_w=None, temperature_c=None, clock_mhz=None,
            pressure_level=1, timestamp=None, sources={},
        )
        pm = _make_pm_with_device(tel)
        pm.telemetry.get_device_snapshot = lambda: broken
        h = _make_handler()
        h._handle_snapshot(pm)
        assert _last_status(h) == 200
        assert _hdr(h, self.HEADER) is None, "无可信时间戳必须省略头（前端标未确认）"


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


# ═══════════════════════════════════════════════════════════════
# 终审修复轮（No-with-fixes：F3 服务端 / F7 / F10）
# ═══════════════════════════════════════════════════════════════


class TestSampleAgeSendTime:
    """F3：采样年龄头须按发送时刻计算 —— 快照组装（DB 读/哈希）耗时
    不得低估年龄（handler 起点的 now 比真实发送早）。"""

    def test_age_computed_at_send_time_not_handler_start(self, monkeypatch):
        t0 = 1_000_000.0
        calls = {"n": 0}

        def fake_time():
            calls["n"] += 1
            return t0 if calls["n"] == 1 else t0 + 30.0

        monkeypatch.setattr(handler_module.time, "time", fake_time)
        tel = _FakeDeviceTel(ts=t0 - 10.0)
        pm = _make_pm_with_device(tel)
        h = _make_handler()
        h._handle_snapshot(pm)
        age = _hdr(h, "X-IFF-Device-Sample-Age")
        assert age is not None
        # 采集期间时钟走了 30s：发送时刻计算 → (t0+30)-(t0-10)=40；
        # 旧实现用 handler 起点的 now → 只报 10（低估）。
        assert int(age) >= 20, f"须按发送时刻计算年龄, got {age}"


class TestSwitchTargetFreshness:
    """F7：在途切换指示不得吃 15s 昂贵缓存的旧值 —— 每轮轮询以
    state.switching_target 即时值覆盖（开始/结束最迟下一轮可见）。"""

    def _pm_with_state(self):
        state = {"switching_target": ""}
        pm = _make_pm()
        pm.mgr.state = SimpleNamespace(
            get_history=lambda n: [],
            get=lambda k, d=None: state.get(k, d),
        )
        return pm, state

    def test_switch_start_visible_within_ttl(self):
        pm, state = self._pm_with_state()
        h1 = _make_handler()
        h1._handle_snapshot(pm)

        state["switching_target"] = "gamma"   # 切换开始（exp TTL 窗口内）
        h2 = _make_handler()
        h2._handle_snapshot(pm)
        assert _last_status(h2) == 200
        snap2 = json.loads(h2._bodies[-1].decode("utf-8"))
        assert snap2["status"].get("switch_target") == "gamma", \
            "切换开始须下一轮即见（15s 缓存不得吞掉）"

    def test_switch_end_clears_and_busts_etag(self):
        pm, state = self._pm_with_state()
        h1 = _make_handler()
        h1._handle_snapshot(pm)
        state["switching_target"] = "gamma"
        h2 = _make_handler()
        h2._handle_snapshot(pm)
        snap2 = json.loads(h2._bodies[-1].decode("utf-8"))

        state["switching_target"] = ""        # 切换结束（TTL 窗口内）
        h3 = _make_handler(headers={"If-None-Match": snap2["etag"]})
        h3._handle_snapshot(pm)
        assert _last_status(h3) == 200, "指示清除改变了内容 → 不得 304"
        snap3 = json.loads(h3._bodies[-1].decode("utf-8"))
        assert not snap3["status"].get("switch_target")


class TestOpenapiDeviceExamples:
    """F10：device 示例必须是合法片段 —— 挂在 DeviceSnapshotEnvelope
    （required=[snapshot, capabilities]）下，不以缺必填字段的形态挂在
    SnapshotResponse（required 含 ts/rev/etag/status/system/models）下。"""

    def _load(self):
        import yaml
        spec_dir = _ROOT / "api-spec"
        openapi = yaml.safe_load((spec_dir / "openapi.yaml").read_text(encoding="utf-8"))
        schemas = yaml.safe_load(
            (spec_dir / "components" / "schemas.yaml").read_text(encoding="utf-8"))
        return openapi, schemas

    def test_examples_live_on_envelope_not_response(self):
        openapi, schemas = self._load()
        resp200 = openapi["paths"]["/api/snapshot"]["get"]["responses"]["200"]
        ex = resp200["content"]["application/json"].get("examples")
        assert not ex, "SnapshotResponse 下的 device-only 示例违反其必填集"

        env = schemas["DeviceSnapshotEnvelope"]
        examples = env.get("examples") or []
        assert len(examples) == 2, "apple/nvidia 两个信封级示例"
        req_env = set(env["required"])
        snap_req = set(schemas["DeviceSnapshot"]["required"])
        for e in examples:
            val = e["value"]
            assert req_env <= set(val.keys()), f"信封示例缺必填: {req_env - set(val.keys())}"
            assert snap_req <= set(val["snapshot"].keys()), "快照示例缺必填"

    def test_apple_pressure_source_is_sysctl(self):
        openapi, schemas = self._load()
        env = schemas["DeviceSnapshotEnvelope"]
        apple = [e for e in env["examples"] if e["value"]["snapshot"]["accelerator"] == "apple_silicon"]
        assert apple, "Apple 示例在场"
        src = apple[0]["value"]["snapshot"]["sources"].get("pressure_level")
        assert src == "sysctl", f"Apple 压力源是 sysctl（kern.memorystatus_vm_pressure_level），got {src}"
