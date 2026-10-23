"""CooldownStore 运行态只读扩展（Phase 1 Task 1/2）。

契约（spec §4.2 / plan Task 1）：
- add() 可选 reason；quarantine() 固定 reason="quarantine_404"
- entries() 锁内快照：{fp: {until, remaining_s, reason, probing}}，含已到期待探测条目
- record_attempt()/last_attempts()：每 fp 最近一次真实传输尝试（ok/status/age_s）
- finish_probe 失败重新冷却保留原 reason；成功删条目
- 既有语义零变化（默认 reason=None；三态 reload/CAS/半开单飞不受影响）

时钟纪律：全部注入 _FakeClock，不碰真实 monotonic（终审 Ruling 教训）。
"""

import pytest

from inferfabric.model_pool.pool_forward import (
    QUARANTINE_TTL_S,
    CooldownStore,
)


class _FakeClock:
    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


class TestReasonEntries:
    def test_add_records_reason_in_entries(self):
        clock = _FakeClock(start=100.0)
        store = CooldownStore()
        store.add("fp-a", now=clock(), reason="cooldown_401")
        e = store.entries(now=clock())
        assert e["fp-a"]["reason"] == "cooldown_401"
        assert e["fp-a"]["until"] == pytest.approx(100.0 + 60.0)
        assert e["fp-a"]["remaining_s"] == pytest.approx(60.0)
        assert e["fp-a"]["probing"] is False

    def test_quarantine_reason_is_404(self):
        clock = _FakeClock()
        store = CooldownStore()
        store.quarantine("fp-q", now=clock())
        e = store.entries(now=clock())
        assert e["fp-q"]["reason"] == "quarantine_404"
        assert e["fp-q"]["remaining_s"] == pytest.approx(QUARANTINE_TTL_S)

    def test_add_without_reason_keeps_none(self):
        clock = _FakeClock()
        store = CooldownStore()
        store.add("fp-b", now=clock())
        assert store.entries(now=clock())["fp-b"]["reason"] is None

    def test_entries_reports_probing_during_half_open(self):
        clock = _FakeClock()
        store = CooldownStore()
        store.add("fp-c", now=clock(), reason="cooldown_401")
        clock.sleep(61.0)  # 过冷却期 → 到期
        assert store.claim_probe("fp-c", now=clock()) is True
        e = store.entries(now=clock())
        # 到期待探测：remaining_s <= 0 且 probing=True
        assert e["fp-c"]["remaining_s"] <= 0
        assert e["fp-c"]["probing"] is True
        # 探测失败 → 重新冷却，probing 清空，reason 保留
        store.finish_probe("fp-c", ok=False, now=clock())
        e = store.entries(now=clock())
        assert e["fp-c"]["probing"] is False
        assert e["fp-c"]["remaining_s"] == pytest.approx(60.0)
        assert e["fp-c"]["reason"] == "cooldown_401"

    def test_entries_omits_recovered(self):
        clock = _FakeClock()
        store = CooldownStore()
        store.add("fp-d", now=clock(), reason="quarantine_404")
        clock.sleep(QUARANTINE_TTL_S + 1.0)
        assert store.claim_probe("fp-d", now=clock()) is True
        store.finish_probe("fp-d", ok=True, now=clock())
        assert "fp-d" not in store.entries(now=clock())

    def test_entries_empty_initially(self):
        store = CooldownStore()
        assert store.entries(now=0.0) == {}


class TestRecordAttempt:
    def test_record_attempt_roundtrip(self):
        clock = _FakeClock(start=1000.0)
        store = CooldownStore()
        store.record_attempt("fp-e", ok=False, status=503, now=clock())
        clock.sleep(180.0)
        la = store.last_attempts(now=clock())
        assert la["fp-e"] == {"ok": False, "status": 503, "age_s": pytest.approx(180.0)}

    def test_record_attempt_overwrites(self):
        clock = _FakeClock()
        store = CooldownStore()
        store.record_attempt("fp-f", ok=False, status=500, now=clock())
        store.record_attempt("fp-f", ok=True, status=200, now=clock())
        la = store.last_attempts(now=clock())
        assert la["fp-f"]["ok"] is True
        assert la["fp-f"]["status"] == 200

    def test_last_attempts_empty_initially(self):
        store = CooldownStore()
        assert store.last_attempts(now=0.0) == {}

    def test_record_attempt_ok_without_status(self):
        clock = _FakeClock()
        store = CooldownStore()
        store.record_attempt("fp-g", ok=True, now=clock())
        la = store.last_attempts(now=clock())
        assert la["fp-g"]["ok"] is True
        assert la["fp-g"]["status"] is None
