"""unit/proxy/test_monitor_report_api.py — /api/monitor-report 上游化（mac prod ②）.

覆盖：parse_monitor_params 参数契约、build_monitor_report 统计口径
（IFFDB 直通）、handler 端点（400/200 映射、TTL 缓存复用）。
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.db import IFFDB
from inferfabric.monitor_report import (
    DEFAULT_WINDOW,
    VALID_SCOPES,
    VALID_WINDOWS,
    MonitorReportCache,
    build_monitor_report,
)


# ═══════════════════════════════════════════════════════════════
# parse_monitor_params（端点参数契约）
# ═══════════════════════════════════════════════════════════════


def test_parse_monitor_params_defaults():
    from inferfabric.proxy.handler import parse_monitor_params
    assert parse_monitor_params("") == (DEFAULT_WINDOW, "all")


def test_parse_monitor_params_valid():
    from inferfabric.proxy.handler import parse_monitor_params
    for w in VALID_WINDOWS:
        assert parse_monitor_params(f"window={w}")[0] == w
    for s in VALID_SCOPES:
        assert parse_monitor_params(f"scope={s}")[1] == s


def test_parse_monitor_params_rejects_unknown():
    from inferfabric.proxy.handler import parse_monitor_params
    with pytest.raises(ValueError):
        parse_monitor_params("window=13m")
    with pytest.raises(ValueError):
        parse_monitor_params("scope=galaxy")


# ═══════════════════════════════════════════════════════════════
# build_monitor_report（统计口径，IFFDB 直通）
# ═══════════════════════════════════════════════════════════════


def _row(now, req_id, model, *, status=200, ttft_ms=100.0, route="local",
         cloud_provider=None, error="", metadata=None, key_name="k1"):
    return {
        "req_id": req_id, "key_name": key_name, "model": model,
        "status": status, "ttft_ms": ttft_ms, "tokens_in": 10,
        "tokens_in_cached": 0, "tokens_out": 20, "agent": "", "ua": "",
        "x_app": "", "duration_ms": 500.0, "route": route,
        "cloud_provider": cloud_provider, "error": error,
        "timestamp": now, "ts": str(now), "metadata": metadata,
    }


@pytest.fixture()
def seeded_db(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    now = time.time()
    db.insert_request_log([
        _row(now - 60, "a1", "glm-5", ttft_ms=100.0),
        _row(now - 60, "a2", "glm-5", ttft_ms=300.0),
        _row(now - 60, "a3", "qwen3-4b-instruct-2507", ttft_ms=None,
             status=502, error="upstream_5xx",
             route="cloud:zhipu", cloud_provider="zhipu"),
        _row(now - 60, "a4", "glm-5", ttft_ms=200.0,
             metadata={"pool_summary": {"pool": "p1",
                                        "final_target": {"actual_model_id": "glm-5.3"}}}),
    ])
    return db


def test_build_report_totals_and_models(seeded_db):
    report = build_monitor_report(seeded_db, window="1h", until=time.time())
    assert report["totals"]["requests"] >= 4
    assert report["coverage"]["truncated"] is False
    models = {m["model"]: m for m in report["models"]}
    assert "glm-5" in models
    # 池行按 actual_model_id 聚合（终审 I4）：glm-5.3 有份，请求别名不改写
    assert "glm-5.3" in models


def test_build_report_missing_ttft_not_counted_as_zero(seeded_db):
    report = build_monitor_report(seeded_db, window="1h", until=time.time())
    models = {m["model"]: m for m in report["models"]}
    q = models["qwen3-4b-instruct-2507"]
    # 缺 TTFT → 分位 None（不是 0ms），样本不计入
    assert q["p50_ttft_ms"] is None
    assert q["ttft_samples"] == 0


def test_build_report_scope_filtering(seeded_db):
    local = build_monitor_report(seeded_db, window="1h", until=time.time(),
                                 scope="local")
    cloud = build_monitor_report(seeded_db, window="1h", until=time.time(),
                                 scope="cloud")
    local_models = {m["model"] for m in local["models"]}
    cloud_models = {m["model"] for m in cloud["models"]}
    assert "qwen3-4b-instruct-2507" not in local_models
    assert "qwen3-4b-instruct-2507" in cloud_models


def test_build_report_pools_from_metadata(seeded_db):
    report = build_monitor_report(seeded_db, window="1h", until=time.time())
    pool_names = {p["pool"] for p in report["pools"]}
    assert "p1" in pool_names


def test_monitor_cache_singleflight_reuse(seeded_db):
    cache = MonitorReportCache(ttl_s=60.0)
    until = time.time()
    r1 = cache.get_or_build(seeded_db, window="1h", until=until)
    r2 = cache.get_or_build(seeded_db, window="1h", until=until)
    assert r1 is r2


# ═══════════════════════════════════════════════════════════════
# handler 端点
# ═══════════════════════════════════════════════════════════════


def _make_handler_with_pm(seeded_db, path):
    import inferfabric.proxy.handler as handler_module
    h = handler_module.ProxyHandler.__new__(handler_module.ProxyHandler)
    h.headers = {}
    h.path = path
    h.command = "GET"
    h._send_json = MagicMock()
    pm = MagicMock()
    pm.telemetry = seeded_db
    pm.metrics.price_config = {}
    return h, pm


def test_endpoint_report_ok(seeded_db):
    import inferfabric.proxy.handler as handler_module
    h, pm = _make_handler_with_pm(
        seeded_db, "/api/monitor-report?window=1h&scope=all")
    h._handle_monitor_report(pm)
    assert h._send_json.call_count == 1
    args, kwargs = h._send_json.call_args
    report = args[0]
    assert "totals" in report and "models" in report
    assert kwargs.get("status", args[1] if len(args) > 1 else 200) == 200


def test_endpoint_bad_window_400(seeded_db):
    import inferfabric.proxy.handler as handler_module
    h, pm = _make_handler_with_pm(
        seeded_db, "/api/monitor-report?window=bogus")
    h._handle_monitor_report(pm)
    args = h._send_json.call_args.args
    assert h._send_json.call_args.args[-1] == 400 or \
        h._send_json.call_args.kwargs.get("status") == 400 or \
        "error" in args[0]
