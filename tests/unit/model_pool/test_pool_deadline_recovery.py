"""绝对截止与半开探测权生命周期 — PLAN-2026-11-15 v2 Task 4（P2）。

契约（计划 Interfaces）：
- 池入口同一单调时钟 deadline_at；逻辑/物理 gate、429 等待、传输耗时共享
  预算，每阶段后重算 — 任何单段等待不得突破总预算。
- 半开探测权全出口释放：预算已尽不领取；未准入/抛异常/取消后可重获，
  且不误记端点失败；并发仅一个真实探测。
- 观察模式限流也尊重池硬截止（不再固定 300s 等待）。
"""

import threading
import time

import pytest

from inferfabric.model_pool.config import CandidateConfig, PoolConfig
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    CooldownStore,
    run_attempts,
)
from inferfabric.model_pool.registry import ResolvedCandidate


def cand(cid, model="qwen3-4b"):
    return CandidateConfig(id=cid, priority=10, kind="local",
                           model_name=model, declared_protocols=("openai",))


def rc(cid):
    return ResolvedCandidate(candidate=cand(cid), endpoint_fingerprint=f"fp-{cid}",
                             evidence={}, route={})


def pool(max_attempts=3, deadline_ms=10000):
    return PoolConfig(name="fast", enabled=True, allowed_locations=("local",),
                      accept_declared=True, max_attempts=max_attempts,
                      deadline_ms=deadline_ms, candidates=(cand("c1"),))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def mono(self):
        return self.t

    def advance(self, dt):
        self.t += dt

    def sleep(self, dt):
        self.t += dt


class TestDeadlineIncludesAllWaits:

    def test_retry_after_recomputed_after_transport(self):
        """传输耗时后重算剩余预算：429 Retry-After 超剩余 → 切候选不等待。"""
        clock = FakeClock()
        sleeps = []
        calls = []

        def transport(c, remaining):
            calls.append(remaining)
            # 真实传输按 remaining 自我约束（socket timeout 语义）
            clock.advance(min(8.0, remaining))
            return AttemptResult(ok=False, committed=False, status=429,
                                 retry_after=5.0)

        outcome = run_attempts(pool(max_attempts=3, deadline_ms=10000),
                               [rc("c1"), rc("c2")], transport,
                               monotonic=clock.mono, sleep=sleeps.append)
        assert len(calls) == 2  # 未按 stale remaining 等待重试原目标
        assert sleeps == []     # 5s 等待 > 重算后的剩余 → 不睡
        assert clock.t <= 1010.0 + 1e-9  # 全程不突破 10s 预算

    def test_retry_after_wait_bounded_by_deadline(self):
        """Retry-After 在剩余预算内 → 等待后重试，总耗时含等待不超预算。"""
        clock = FakeClock()
        calls = []

        def transport(c, remaining):
            calls.append(1)
            return AttemptResult(ok=False, committed=False, status=429,
                                 retry_after=3.0)

        outcome = run_attempts(pool(max_attempts=2, deadline_ms=10000),
                               [rc("c1")], transport,
                               monotonic=clock.mono, sleep=clock.sleep)
        assert len(calls) == 2
        assert clock.t == 1003.0  # 等待 3s 后同目标重试成功路径
        assert outcome.attempts[-1].status == 429 or outcome.ok

    def test_stream_chunks_cannot_extend_budget(self):
        """每次传输耗掉大块时间：deadline 到 → 停止，不无限续期。"""
        clock = FakeClock()
        calls = []

        def transport(c, remaining):
            calls.append(remaining)
            clock.advance(6.0)  # 每次调用耗 6s（模拟慢流分片）
            return AttemptResult(ok=False, committed=False, status=None,
                                 error="slow stream")

        outcome = run_attempts(pool(max_attempts=10, deadline_ms=10000),
                               [rc("c1")], transport,
                               monotonic=clock.mono, sleep=clock.sleep)
        # 10s 预算、每次 6s → 最多 2 次真实调用（10/6 → 第 3 次前 remaining<=0）
        assert len(calls) <= 2
        assert clock.t <= 1012.0

    def test_observe_mode_respects_deadline(self):
        """观察模式物理门等待受 timeout 约束，不再固定 300s。"""
        from inferfabric.ratelimit import DualGateLimiter
        gate = DualGateLimiter.__new__(DualGateLimiter)
        # 观察模式 + 全局并发已占满（value=0）
        gate._mode = "observe"
        gate._timeout = 300
        gate._global_sem = threading.Semaphore(0)
        gate._per_model = {}
        gate._rpm = None
        sem = threading.Semaphore(0)
        gate._get_or_create_sem = lambda m: sem
        gate._rpm = SimpleNamespaceRPM()
        t0 = time.monotonic()
        res = gate.acquire("m", timeout=0.3)
        elapsed = time.monotonic() - t0
        assert elapsed < 10  # 不是 300s
        assert res.ok is True  # 观察模式放行（不强制）


class SimpleNamespaceRPM:
    def try_acquire(self, model):
        return True, ""

    def acquire(self, model, timeout):
        return True, ""

    def release(self, model):
        pass


class TestHalfOpenReleaseAllExits:

    def test_budget_exhausted_does_not_claim_probe(self):
        """预算/次数已尽 → 不领取下一候选的探测权。"""
        store = CooldownStore(ttl_s=10)
        base = 1000.0
        now = {"t": base}
        # c2 端点冷却已到期（半开窗口）
        store.add("fp-c2", now=base - 20)
        calls = []

        def transport(c, remaining):
            calls.append(c.candidate.id)
            return AttemptResult(ok=False, committed=False, status=None,
                                 error="conn fail")

        outcome = run_attempts(pool(max_attempts=1),
                               [rc("c1"), rc("c2")], transport,
                               cooldowns=store, monotonic=lambda: now["t"])
        assert calls == ["c1"]  # max_attempts=1 → c2 不被领取
        entries = store.entries(now=base)
        assert entries["fp-c2"]["probing"] is False

    def test_admission_failure_releases_probe_without_penalty(self):
        """未准入 → 释放探测权且不重新冷却（不误记端点失败）。"""
        store = CooldownStore(ttl_s=10)
        base = 1000.0
        store.add("fp-c1", now=base - 20)  # 到期
        claimed = []

        def transport(c, remaining):
            claimed.append(1)
            return AttemptResult(ok=False, committed=False, admitted=False,
                                 error="gate timeout")

        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base)
        assert claimed == [1]
        entries = store.entries(now=base)
        assert entries["fp-c1"]["probing"] is False   # 探测权可重获
        assert entries["fp-c1"]["remaining_s"] <= 0   # 未被重新冷却
        assert store.claim_probe("fp-c1", now=base) is True  # 可再次领取

    def test_transport_exception_releases_probe(self):
        """传输抛异常 → 探测权释放，按连接失败切下一候选。"""
        store = CooldownStore(ttl_s=10)
        base = 1000.0
        store.add("fp-c1", now=base - 20)
        calls = []

        def transport(c, remaining):
            calls.append(c.candidate.id)
            if c.candidate.id == "c1":
                raise RuntimeError("socket exploded")
            return AttemptResult(ok=True, committed=True, status=200)

        outcome = run_attempts(pool(), [rc("c1"), rc("c2")], transport,
                               cooldowns=store, monotonic=lambda: base)
        assert calls == ["c1", "c2"]
        assert outcome.ok is True
        assert store.entries(now=base)["fp-c1"]["probing"] is False

    def test_concurrent_probe_single_flight(self):
        """并发 claim：仅一个真实半开探测。"""
        store = CooldownStore(ttl_s=10)
        base = 1000.0
        store.add("fp-x", now=base - 20)
        results = []
        barrier = threading.Barrier(4)

        def try_claim():
            barrier.wait()
            results.append(store.claim_probe("fp-x", now=base))

        threads = [threading.Thread(target=try_claim) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1

    def test_failed_probe_still_recools(self):
        """真实探测失败仍重新冷却（既有语义不回归）。"""
        store = CooldownStore(ttl_s=10)
        base = 1000.0
        store.add("fp-c1", now=base - 20)

        def transport(c, remaining):
            return AttemptResult(ok=False, committed=False, status=503)

        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base)
        entries = store.entries(now=base)
        assert entries["fp-c1"]["remaining_s"] > 0  # 重新冷却
        assert entries["fp-c1"]["probing"] is False

    def test_transport_exception_consumes_attempt(self):
        """终审 I7：异常 = 可能已发出的真实尝试 → admitted=True 并消耗
        max_attempts（attempt_count 不再少报，物理调用数受预算约束）。"""
        calls = []

        def transport(c, remaining):
            calls.append(c.candidate.id)
            if c.candidate.id in ("c1", "c2"):
                raise RuntimeError("read timeout after send")
            return AttemptResult(ok=True, committed=True, status=200)

        outcome = run_attempts(pool(max_attempts=2),
                               [rc("c1"), rc("c2"), rc("c3")], transport,
                               monotonic=lambda: 1000.0)
        assert calls == ["c1", "c2"]          # 预算耗尽，c3 不再尝试
        assert outcome.ok is False
        admitted = [r for r in outcome.attempts if r.admitted]
        assert len(admitted) == 2             # 计入尝试，不少报
        assert all(r.status is None for r in admitted)
