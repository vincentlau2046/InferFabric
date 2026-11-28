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


# ═══════════════════════════════════════════════════════════════
# D1：同快照聚合增量（cache 份额 / 模型成本 / agents / series 三路）
# ═══════════════════════════════════════════════════════════════


def _drow(now, req_id, model, *, agent="", cached=0, tin=10, tout=20, **kw):
    # 默认路由对齐生产写法 responses_bridge.py:309 `local:<model_name>`
    kw.setdefault("route", f"local:{model}")
    r = _row(now, req_id, model, **kw)
    r["tokens_in"] = tin
    r["tokens_in_cached"] = cached
    r["agent"] = agent
    return r


@pytest.fixture()
def d1_db(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    now = time.time()
    db.insert_request_log([
        # 本地 glm-5：3 行（cached 5/10/0）
        _drow(now - 60, "b1", "glm-5", agent="alice", cached=5),
        _drow(now - 120, "b2", "glm-5", agent="alice", cached=10),
        _drow(now - 180, "b3", "glm-5", agent="bob"),
        # 云端 zhipu 同名 glm-5（location+provider 身份分离）
        _drow(now - 60, "b4", "glm-5", agent="alice", cached=2,
              route="cloud:zhipu", cloud_provider="zhipu"),
        # 未识别客户端（agent 空）+ 独立模型
        _drow(now - 60, "b5", "qwen3", agent="", cached=1),
    ])
    return db, now


def test_d1_compat_keys_unchanged(d1_db):
    """既有顶层与 totals/models 键集不变（D2 消费面兼容钉住）。"""
    db, now = d1_db
    report = build_monitor_report(db, window="1h", until=now)
    for k in ("contract", "window", "scope", "since", "until", "collected_at",
              "coverage", "totals", "models", "series", "pools", "errors"):
        assert k in report, k
    for k in ("requests", "success", "success_rate", "tokens_in", "tokens_out",
              "usage_unknown_rows", "cost_estimate", "cost_coverage"):
        assert k in report["totals"], k
    m = report["models"][0]
    for k in ("location", "provider", "model", "requests", "success",
              "tokens_in", "tokens_out", "p50_ttft_ms", "p95_ttft_ms",
              "ttft_samples", "p50_duration_ms", "p95_duration_ms",
              "duration_samples", "success_rate"):
        assert k in m, k
    b = report["series"][0]
    for k in ("bucket_start", "bucket_end", "requests", "success",
              "tokens_in", "tokens_out"):
        assert k in b, k


def test_d1_cache_share_totals_and_models(d1_db):
    db, now = d1_db
    report = build_monitor_report(db, window="1h", until=now)
    t = report["totals"]
    assert t["tokens_in_cached"] == 5 + 10 + 0 + 2 + 1
    assert abs(t["cache_share"] - t["tokens_in_cached"] / t["tokens_in"]) < 1e-9
    models = {(m["location"], m["model"]): m for m in report["models"]}
    glm_local = models[("local", "glm-5")]
    assert glm_local["tokens_in_cached"] == 15
    assert abs(glm_local["cache_share"] - 15 / 30) < 1e-9


def test_d1_cache_share_null_without_denominator(tmp_path):
    """分母无数据 → 无值（null），不冒认 0。"""
    db = IFFDB(data_dir=tmp_path)
    now = time.time()
    db.insert_request_log([_drow(now - 60, "c1", "glm-5", tin=0, tout=0)])
    report = build_monitor_report(db, window="1h", until=now)
    assert report["totals"]["cache_share"] is None
    m = report["models"][0]
    assert m["cache_share"] is None


def test_d1_same_name_different_provider_separate(d1_db):
    """同名模型跨厂商不混合（既有身份规则在增量输出下保持）。"""
    db, now = d1_db
    report = build_monitor_report(db, window="1h", until=now)
    models = {(m["location"], m["model"]): m for m in report["models"]}
    assert models[("local", "glm-5")]["requests"] == 3
    assert models[("cloud", "glm-5")]["requests"] == 1


def test_d1_model_cost_estimate_priced_vs_unpriced(d1_db):
    """模型补 cost_estimate：已配价云端行有值；本地/未配价 → null（不归 0）。"""
    from inferfabric.metrics_aggregator import CloudModelPrice
    db, now = d1_db
    prices = {"glm-5": CloudModelPrice(price_input=2.0, price_output=8.0)}
    report = build_monitor_report(db, window="1h", until=now, prices=prices)
    models = {(m["location"], m["model"]): m for m in report["models"]}
    cloud = models[("cloud", "glm-5")]
    local = models[("local", "glm-5")]
    # 云端 glm-5 单行：tin=10, tout=20 → 10/1e6*2 + 20/1e6*8 = 0.00018
    assert abs(cloud["cost_estimate"] - 0.00018) < 1e-9
    assert local["cost_estimate"] is None
    assert models[("local", "qwen3")]["cost_estimate"] is None
    # totals 联动不变
    assert abs(report["totals"]["cost_estimate"] - 0.00018) < 1e-9
    assert report["totals"]["cost_coverage"] == "priced"


def test_d1_agents_grouping_with_unidentified(d1_db):
    """agents[] 按既有客户端 ID（agent 字段）汇总；空 → 保留 unidentified 分组。"""
    db, now = d1_db
    report = build_monitor_report(db, window="1h", until=now)
    agents = {a["agent"]: a for a in report["agents"]}
    assert set(agents) == {"alice", "bob", "unidentified"}
    alice = agents["alice"]
    assert alice["requests"] == 3
    assert alice["tokens_in_cached"] == 5 + 10 + 2
    assert abs(alice["cache_share"] - 17 / 30) < 1e-9
    assert agents["bob"]["requests"] == 1
    assert agents["unidentified"]["requests"] == 1


def test_d1_agents_respect_scope(d1_db):
    db, now = d1_db
    local = build_monitor_report(db, window="1h", until=now, scope="local")
    agents = {a["agent"]: a for a in local["agents"]}
    assert agents["alice"]["requests"] == 2      # 云端行被 scope 过滤
    assert agents["unidentified"]["requests"] == 1


def test_d1_series_location_breakdown(d1_db):
    """series[] 每桶增 local/cloud/unknown 三路统计；三路合计 == 桶总量。"""
    db, now = d1_db
    report = build_monitor_report(db, window="1h", until=now)
    b = report["series"][-1]   # 全部行都落在最后一个桶
    for loc in ("local", "cloud", "unknown"):
        d = b[loc]
        for k in ("requests", "tokens_in", "tokens_out", "tokens_in_cached",
                  "cost_estimate"):
            assert k in d, (loc, k)
    assert sum(b[loc]["requests"] for loc in ("local", "cloud", "unknown")) == 5
    assert b["requests"] == 5
    assert b["local"]["tokens_in_cached"] == 16
    assert b["cloud"]["tokens_in_cached"] == 2
    assert b["local"]["cost_estimate"] is None


def test_d1_series_cost_with_prices(d1_db):
    from inferfabric.metrics_aggregator import CloudModelPrice
    db, now = d1_db
    prices = {"glm-5": CloudModelPrice(price_input=2.0, price_output=8.0)}
    report = build_monitor_report(db, window="1h", until=now, prices=prices)
    b = report["series"][-1]
    assert abs(b["cloud"]["cost_estimate"] - 0.00018) < 1e-9
    assert b["local"]["cost_estimate"] is None   # 本地不计价，不冒认累计


def test_d1_empty_window_nulls(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    now = time.time()
    report = build_monitor_report(db, window="1h", until=now)
    assert report["agents"] == []
    assert report["totals"]["cache_share"] is None
    assert report["totals"]["tokens_in_cached"] == 0


def test_d1_scan_budget_not_regressed(d1_db):
    """增量字段不破坏键集分页/截断契约（上限断言：全量扫完 truncated=False）。"""
    db, now = d1_db
    report = build_monitor_report(db, window="1h", until=now)
    assert report["coverage"]["truncated"] is False
    assert report["coverage"]["rows_scanned"] == 5
