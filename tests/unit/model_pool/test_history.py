"""model_pool — P3 数据底盘：CooldownStore 事件环 + PoolHistory 延迟环。

核心不变量：
- 事件环：add→cooldown、quarantine→quarantine（不双记 cooldown）、
  claim_probe 授予到期探测→probe_start（健康端点放行不记）、
  finish_probe 注册态回写→probe_pass/probe_fail（重新冷却不双记）
- 事件环全局上限 256，新在后，超限淘汰最旧
- PoolHistory 只记 ok 且 ttft_ms 非 None 的真实请求；每候选环上限 40；
  snapshot 按池/候选分组（注入钟可测 age_s/wall_ts）
"""

import itertools

from inferfabric.model_pool.pool_forward import CooldownStore

from inferfabric.model_pool.history import PoolHistory


class TestCooldownEvents:
    def test_cooldown_events_lifecycle(self):
        cd = CooldownStore()
        t0 = 100.0
        cd.add("fp1", now=t0, reason="cooldown_401")
        t1 = t0 + 1
        cd.quarantine("fp2", now=t1)
        # 到期（隔离 TTL 1800s）→ 授予单飞探测 → 回写成功
        t2 = t1 + 1800.0 + 1
        assert cd.claim_probe("fp2", now=t2) is True
        t3 = t2 + 5
        cd.finish_probe("fp2", ok=True, now=t3)
        t4 = t3 + 10
        events = cd.events(now=t4)

        assert [e["type"] for e in events] == [
            "cooldown", "quarantine", "probe_start", "probe_pass"]
        assert [e["fingerprint"] for e in events] == [
            "fp1", "fp2", "fp2", "fp2"]
        assert events[0]["reason"] == "cooldown_401"
        # age_s：越早的事件越老；wall_ts 单调不减
        assert events[0]["age_s"] > events[-1]["age_s"]
        walls = [e["wall_ts"] for e in events]
        assert walls == sorted(walls)

    def test_cooldown_events_probe_fail_and_no_event_for_healthy_grant(self):
        cd = CooldownStore()
        # 健康端点（无条目）claim_probe 放行 → 不产生事件
        assert cd.claim_probe("fp-ok", now=1.0) is True
        assert cd.events(now=2.0) == []
        # 探测失败 → probe_fail（重新冷却不双记 cooldown）
        cd.add("fp9", now=3.0)
        assert cd.claim_probe("fp9", now=3.0 + 60.0) is True
        cd.finish_probe("fp9", ok=False, now=3.0 + 61.0)
        assert [e["type"] for e in cd.events(now=200.0)] == [
            "cooldown", "probe_start", "probe_fail"]

    def test_cooldown_events_ring_capped_256(self):
        cd = CooldownStore()
        for i in range(300):
            cd.add(f"fp-{i}", now=float(i))
        events = cd.events(now=10000.0)
        assert len(events) == 256
        # 保留最新：最旧留存 = fp-44，最新 = fp-299
        assert events[0]["fingerprint"] == "fp-44"
        assert events[-1]["fingerprint"] == "fp-299"


class TestPoolHistory:
    def test_pool_history_records_only_ok_with_ttft(self):
        clock = iter([10.0, 11.0]).__next__
        h = PoolHistory(clock=clock)
        h.record("p", "c1", ttft_ms=None, total_ms=5.0, ok=True)    # 无 TTFT 不记
        h.record("p", "c1", ttft_ms=100.0, total_ms=200.0, ok=False)  # 失败不记
        h.record("p", "c1", ttft_ms=100.0, total_ms=200.0, ok=True)   # 记 @10
        h.record("p", "c2", ttft_ms=150.0, total_ms=250.0, ok=True)   # 记 @11
        snap = h.snapshot(now=20.0)
        assert set(snap) == {"p"}
        assert set(snap["p"]) == {"c1", "c2"}
        e1, e2 = snap["p"]["c1"], snap["p"]["c2"]
        assert len(e1) == 1 and len(e2) == 1
        assert e1[0]["ttft_ms"] == 100.0 and e1[0]["total_ms"] == 200.0
        assert e1[0]["age_s"] == 10.0 and e1[0]["wall_ts"] == 10.0
        assert e2[0]["age_s"] == 9.0

    def test_pool_history_ring_capped_40_per_candidate(self):
        h = PoolHistory(clock=itertools.count(100.0).__next__)
        for i in range(45):
            h.record("p", "c1", ttft_ms=float(i), total_ms=1.0, ok=True)
        snap = h.snapshot(now=1000.0)
        ring = snap["p"]["c1"]
        assert len(ring) == 40
        # 保留最新：最旧留存 = 第 6 条（i=5），最新 = i=44
        assert ring[0]["ttft_ms"] == 5.0
        assert ring[-1]["ttft_ms"] == 44.0

    def test_pool_history_snapshot_empty_when_no_records(self):
        assert PoolHistory().snapshot() == {}
