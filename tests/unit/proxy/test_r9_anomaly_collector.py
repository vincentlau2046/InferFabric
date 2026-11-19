"""
unit/proxy/test_r9_anomaly_collector.py — R9: AnomalyCollector 异常事件采集

测试对象:
  - inferfabric.anomaly_collector.AnomalyEvent
  - inferfabric.anomaly_collector.AnomalyCollector
  - inferfabric.proxy.handler.ProxyHandler._handle_anomalies

覆盖范围:
  - record → query 返回完整事件
  - 环形缓冲溢出：501 条保留 500 条
  - query 按 category / severity / since 过滤
  - clear 清空
  - /api/anomalies 端点 JSON 格式
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from inferfabric.anomaly_collector import AnomalyEvent, AnomalyCollector


# ═══════════════════════════════════════════════════════════════
# 1. AnomalyEvent
# ═══════════════════════════════════════════════════════════════


def test_anomaly_event_defaults():
    """空构造的 event 有合理默认值。"""
    e = AnomalyEvent()
    assert e.id == ""
    assert e.ts == 0.0
    assert e.category == ""
    assert e.severity == ""
    assert e.model == ""
    assert e.message == ""
    assert e.status_code == 0
    assert e.possible_cause == ""
    assert e.detail is None


def test_anomaly_event_full_construction():
    """完整字段构造。"""
    e = AnomalyEvent(
        category="routing",
        severity="warning",
        model="xyz-v42",
        message="Unknown model rejected",
        status_code=404,
        possible_cause="Model not configured",
        detail={"active_services": []},
    )
    assert e.category == "routing"
    assert e.severity == "warning"
    assert e.status_code == 404
    assert e.detail == {"active_services": []}


# ═══════════════════════════════════════════════════════════════
# 2. AnomalyCollector
# ═══════════════════════════════════════════════════════════════


def test_record_and_query():
    """record 一条，query 返回该事件。"""
    c = AnomalyCollector(maxsize=100)
    c.record(AnomalyEvent(category="routing", severity="warning",
                          model="test", message="test event", status_code=404))
    assert c.count() == 1
    results = c.query()
    assert len(results) == 1
    assert results[0].message == "test event"
    assert results[0].category == "routing"
    assert results[0].id != ""  # auto-generated
    assert results[0].ts > 0


def test_record_sets_id_and_ts():
    """record 自动设置 id 和 ts（即使传入空值）。"""
    c = AnomalyCollector()
    e = AnomalyEvent(message="no id")
    assert e.id == ""
    assert e.ts == 0.0
    c.record(e)
    assert e.id != ""
    assert e.ts > 0
    assert "-" in e.id  # format: hex_ms-hex_counter


def test_record_id_monotonic():
    """连续 record 的 id 计数器递增。"""
    c = AnomalyCollector()
    e1 = AnomalyEvent(message="first")
    e2 = AnomalyEvent(message="second")
    c.record(e1)
    c.record(e2)
    id1 = e1.id.split("-")[-1]
    id2 = e2.id.split("-")[-1]
    assert int(id2, 16) == int(id1, 16) + 1  # monotonic


def test_ring_buffer_overflow():
    """超过 maxsize 时淘汰最旧事件。"""
    c = AnomalyCollector(maxsize=5)
    for i in range(6):
        c.record(AnomalyEvent(message=f"event-{i}", model="test"))
    assert c.count() == 5
    messages = [e.message for e in c.query()]
    assert "event-0" not in messages  # 最旧的被淘汰
    assert "event-5" in messages  # 最新的保留


def test_query_limit():
    """query 的 limit 参数截断返回。"""
    c = AnomalyCollector(maxsize=100)
    for i in range(10):
        c.record(AnomalyEvent(message=f"e-{i}"))
    assert len(c.query(limit=3)) == 3


def test_query_since():
    """query 的 since 参数按时间过滤。"""
    c = AnomalyCollector()
    c.record(AnomalyEvent(message="old"))
    time.sleep(0.01)   # clock resolution is µs but >= comparison needs strict separation
    t = time.time()
    time.sleep(0.01)
    c.record(AnomalyEvent(message="new"))
    results = c.query(since=t)
    assert len(results) == 1
    assert results[0].message == "new"


def test_query_category_filter():
    """按 category 过滤。"""
    c = AnomalyCollector()
    c.record(AnomalyEvent(category="routing", message="r1"))
    c.record(AnomalyEvent(category="model", message="m1"))
    c.record(AnomalyEvent(category="routing", message="r2"))
    results = c.query(category="routing")
    assert len(results) == 2
    assert all(e.category == "routing" for e in results)


def test_query_severity_filter():
    """按 severity 过滤。"""
    c = AnomalyCollector()
    c.record(AnomalyEvent(severity="info", message="i1"))
    c.record(AnomalyEvent(severity="error", message="e1"))
    results = c.query(severity="error")
    assert len(results) == 1
    assert results[0].severity == "error"


def test_query_combined_filters():
    """category + severity 组合过滤。"""
    c = AnomalyCollector()
    c.record(AnomalyEvent(category="routing", severity="warning", message="rw1"))
    c.record(AnomalyEvent(category="routing", severity="error", message="re1"))
    c.record(AnomalyEvent(category="model", severity="error", message="me1"))
    results = c.query(category="routing", severity="error")
    assert len(results) == 1
    assert results[0].message == "re1"


def test_query_reverse_time_order():
    """query 结果按时间倒序（最新的在前）。"""
    c = AnomalyCollector()
    times = []
    for i in range(3):
        c.record(AnomalyEvent(message=f"e-{i}"))
        times.append(time.time())
        if i < 2:
            time.sleep(0.01)
    results = c.query()
    # 越晚的 record，ts 越大 → 排在前面
    assert results[0].message == "e-2"
    assert results[-1].message == "e-0"


def test_clear():
    """clear 清空所有事件。"""
    c = AnomalyCollector()
    c.record(AnomalyEvent(message="x"))
    assert c.count() == 1
    c.clear()
    assert c.count() == 0
    assert len(c.query()) == 0


def test_count_empty():
    """新建的 collector count==0。"""
    c = AnomalyCollector()
    assert c.count() == 0


def test_concurrent_record(monkeypatch):
    """多线程并发 record 不丢数据（线程安全）。"""
    import threading
    c = AnomalyCollector(maxsize=5000)
    n_threads = 4
    events_per_thread = 100

    def worker():
        for i in range(events_per_thread):
            c.record(AnomalyEvent(message=f"t{i}"))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert c.count() == n_threads * events_per_thread


# ═══════════════════════════════════════════════════════════════
# 3. /api/anomalies 端点
# ═══════════════════════════════════════════════════════════════


class _FakePM:
    """最小化的 pm 替身（仅用于测试 _handle_anomalies）。"""
    def __init__(self):
        from inferfabric.anomaly_collector import AnomalyCollector
        self.anomalies = AnomalyCollector()


class _FakeWFile:
    def __init__(self):
        self.data = b""
    def write(self, b):
        self.data += b
    def flush(self):
        pass


class _FakeHandler:
    def __init__(self, path="/api/anomalies"):
        self.path = path
        self.wfile = _FakeWFile()
        self.status = None
        self.sent_headers = {}
        self.headers = {}
    def send_response(self, status):
        self.status = status
    def send_header(self, k, v):
        self.sent_headers[k] = str(v)
    def end_headers(self):
        pass
    def _send_json(self, data, status=200, extra_headers=None):
        from inferfabric import forwarder
        forwarder.send_json(self, data, status, extra_headers=extra_headers)


def test_anomalies_endpoint_empty():
    """没有异常事件时 /api/anomalies 返回空列表。"""
    import inferfabric.proxy.handler as handler_mod
    pm = _FakePM()
    h = _FakeHandler()
    handler_mod.ProxyHandler._handle_anomalies(h, pm)
    assert h.status == 200
    body = h.wfile.data.decode()
    import json
    data = json.loads(body)
    assert data["events"] == []
    assert data["count"] == 0


def test_anomalies_endpoint_with_events():
    """有异常事件时 /api/anomalies 返回完整事件。"""
    import inferfabric.proxy.handler as handler_mod
    import json
    pm = _FakePM()
    pm.anomalies.record(AnomalyEvent(
        category="routing", severity="error", model="test",
        message="something broke", status_code=500,
        possible_cause="OOM or config error",
    ))
    h = _FakeHandler()
    handler_mod.ProxyHandler._handle_anomalies(h, pm)
    assert h.status == 200
    data = json.loads(h.wfile.data.decode())
    assert data["count"] == 1
    e = data["events"][0]
    assert e["category"] == "routing"
    assert e["severity"] == "error"
    assert e["model"] == "test"
    assert e["message"] == "something broke"
    assert e["status_code"] == 500
    assert e["possible_cause"] == "OOM or config error"

def test_anomalies_endpoint_filter():
    """/api/anomalies?category=routing 过滤。"""
    import inferfabric.proxy.handler as handler_mod
    import json
    pm = _FakePM()
    pm.anomalies.record(AnomalyEvent(category="routing", message="r1"))
    pm.anomalies.record(AnomalyEvent(category="model", message="m1"))
    h = _FakeHandler(path="/api/anomalies?category=routing")
    handler_mod.ProxyHandler._handle_anomalies(h, pm)
    assert h.status == 200
    data    =         json.loads(h.wfile.data.decode())
    assert data["count"] == 1
    assert data["events"][0]["message"] == "r1"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))