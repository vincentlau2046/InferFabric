"""TelemetryHub query_request_log 游标直通（PR #38 生产 follow-up）.

monitor_report 经 pm.telemetry 走键集分页（before_ts/before_id）——
包装层直通签名缺参 → 500 "monitor report unavailable"（真机 2026-11-22）。
钉死：两处直通（legacy + hub）必须透传游标 kwargs。
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from inferfabric.db import IFFDB
from inferfabric.telemetry import TelemetryHub


@pytest.fixture()
def hub(tmp_path):
    db = IFFDB(data_dir=tmp_path)
    now = time.time()
    db.insert_request_log([
        {"req_id": f"c{i}", "key_name": "k", "model": "m", "status": 200,
         "ttft_ms": 10.0 + i, "tokens_in": 1, "tokens_out": 2,
         "duration_ms": 5.0, "route": "local", "error": "",
         "timestamp": now - i, "ts": str(now - i)}
        for i in range(5)
    ])
    return TelemetryHub(tmp_path), db


def test_hub_keyset_pagination_passthrough(hub):
    """before_ts/before_id 透传：hub 与裸 db 返回一致（游标语义）。"""
    hub_t, db = hub
    rows_all = hub_t.query_request_log(since=0, limit=100)
    assert len(rows_all) == 5
    first = rows_all[0]
    page2_hub = hub_t.query_request_log(since=0, limit=100,
                                        before_ts=first["timestamp"],
                                        before_id=first["id"])
    page2_db = db.query_request_log(since=0, limit=100,
                                    before_ts=first["timestamp"],
                                    before_id=first["id"])
    assert page2_hub == page2_db
    assert len(page2_hub) == 4
    # 游标语义：首页行被排除（timestamp < 游标）；id tiebreak 仅约束同秒行
    assert first["req_id"] not in {r["req_id"] for r in page2_hub}


def test_hub_keyset_same_second_tiebreak(hub):
    """同秒海量行：id tiebreak 保证页间无重叠不漏不计重。"""
    hub_t, db = hub
    now = time.time()
    same_ts = now - 1000.0
    db.insert_request_log([
        {"req_id": f"s{i}", "key_name": "k", "model": "m", "status": 200,
         "ttft_ms": 1.0, "tokens_in": 1, "tokens_out": 1,
         "duration_ms": 1.0, "route": "local", "error": "",
         "timestamp": same_ts, "ts": str(same_ts)}
        for i in range(7)
    ])
    page1 = hub_t.query_request_log(since=0, limit=4)
    assert len(page1) == 4
    cur = page1[-1]
    page2 = hub_t.query_request_log(since=0, limit=100,
                                    before_ts=cur["timestamp"],
                                    before_id=cur["id"])
    ids1 = {r["id"] for r in page1}
    ids2 = {r["id"] for r in page2}
    assert not ids1 & ids2            # 无重叠
    assert len(ids1 | ids2) == 5 + 7  # 不漏不计重


def test_hub_monitor_report_builds_through_telemetry(hub):
    """端到端：build_monitor_report 以 hub 为 db 直通可跑（原 500 路径）。"""
    from inferfabric.monitor_report import build_monitor_report
    hub_t, _ = hub
    report = build_monitor_report(hub_t, window="1h", until=time.time())
    assert report["totals"]["requests"] == 5


def test_hub_window_mode_passthrough(hub):
    """B1 window 模式读侧：hub 直通与裸 db 返回完全一致（含计数）。"""
    hub_t, db = hub
    hub_res = hub_t.query_request_log_window(since=0, until=time.time() + 60,
                                             limit=3)
    db_res = db.query_request_log_window(since=0, until=time.time() + 60,
                                         limit=3)
    assert hub_res == db_res
    assert hub_res["total"] == 5
    assert len(hub_res["logs"]) == 3
    assert hub_res["failed_total"] == 0
