"""Unified-memory ResourceLedger backend (Apple Silicon).

Behaviour contract (R1/R2/R3, budget B = RAM − reserve) is the
single source of truth for unified-memory admission. Probes are injected by the caller
(production: platform.probe; tests: fixed stubs) — no subprocess calls here.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .resource import (
    AdmitDecision,
    LedgerEntry,
    Lifecycle,
    Mode,
    ProbeCallable,
    ResourceLedger,
)


class UnifiedMemoryLedger(ResourceLedger):
    """统一内存账本：准入（R1）+ 常驻策略（R2）+ 压力安全网（R3）。

    `probe_available_mb` 注入当前可用水位探测（生产=platform.probe
    mem_used_mb 反推，测试=固定桩）；None 表示探测不可用，此时 R1 仅按账面
    基线记账。
    """

    def __init__(
        self,
        budget_mb: int,
        probe_available_mb: Optional[ProbeCallable] = None,
    ):
        self.budget_mb = budget_mb
        self.probe_available_mb = probe_available_mb
        self._entries: dict[str, LedgerEntry] = {}

    # ── 记账 ────────────────────────────────────────────────────

    def registered(self, name: str) -> Optional[LedgerEntry]:
        return self._entries.get(name)

    def entries(self) -> dict[str, LedgerEntry]:
        return dict(self._entries)

    def resident_total_mb(self) -> int:
        """账面占用：loading/ready/sleeping 的模型都占内存（sleeping 保留
        基线的 25% 作 KV 归零后的驻留估计，与 upstream sleep 语义对齐）。"""
        total = 0
        for e in self._entries.values():
            if e.state == Lifecycle.STOPPED:
                continue
            total += e.mem_baseline_mb if e.state != Lifecycle.SLEEPING \
                else max(e.mem_baseline_mb // 4, 0)
        return total

    def register(self, name: str, mem_baseline_mb: int, mode: Mode,
                 idle_timeout_min: float = 30.0) -> LedgerEntry:
        e = LedgerEntry(name=name, mem_baseline_mb=mem_baseline_mb,
                        mode=mode, idle_timeout_min=idle_timeout_min)
        self._entries[name] = e
        return e

    # ── R1 准入 ────────────────────────────────────────────────

    def admit(self, name: str, force: bool = False) -> AdmitDecision:
        """准入检查。ok=False 时 reason 面向用户（弹窗文案直接可用）。"""
        e = self._entries.get(name)
        if e is None:
            return AdmitDecision(False, f"模型 {name} 未注册到内存账本")
        if e.state in (Lifecycle.READY, Lifecycle.LOADING):
            return AdmitDecision(True, "已在运行")
        if e.state == Lifecycle.SLEEPING:
            return AdmitDecision(True, "休眠中，唤醒即可")

        needed = e.mem_baseline_mb
        accounted = self.resident_total_mb()
        available = self.budget_mb - accounted

        # 实测水位修正：探测可用时取两者更紧的约束（账面 vs 实测）
        if self.probe_available_mb is not None:
            live = self.probe_available_mb()
            if live is not None:
                available = min(available, live)

        if needed <= available:
            e.state = Lifecycle.LOADING
            return AdmitDecision(True, "通过", available, needed)

        if force:
            # 用户确认后的超卖放行——责任在人（R1 语义）
            e.state = Lifecycle.LOADING
            return AdmitDecision(True, f"用户确认超卖加载（超出 {needed - available}MB）",
                                 available, needed)
        return AdmitDecision(
            False,
            f"内存不足：需要 {needed}MB，预算仅剩 {available}MB"
            f"（B = RAM − 保留 = {self.budget_mb}MB，已占用 {accounted}MB）",
            available, needed,
        )

    # ── 状态迁移 ────────────────────────────────────────────────

    def mark_ready(self, name: str) -> None:
        e = self._entries[name]
        e.state = Lifecycle.READY
        e.last_used_ts = time.time()

    def mark_sleeping(self, name: str) -> None:
        e = self._entries[name]
        e.state = Lifecycle.SLEEPING

    def mark_stopped(self, name: str) -> None:
        e = self._entries[name]
        e.state = Lifecycle.STOPPED

    def touch(self, name: str) -> None:
        """请求经过时刷新 last_used（proxy 转发成功后调用）。"""
        e = self._entries.get(name)
        if e:
            e.last_used_ts = time.time()

    # ── R2 空闲休眠候选 ────────────────────────────────────────

    def idle_sleep_candidates(self, now: Optional[float] = None) -> list[str]:
        """按 R2：on_demand 且空闲超过 idle_timeout_min 的 ready 模型。

        返回按空闲时长降序（最久未用先休）。resident 永不进入候选。
        """
        now = now or time.time()
        out = []
        for e in self._entries.values():
            if (e.mode is Mode.ON_DEMAND and e.state is Lifecycle.READY
                    and (now - e.last_used_ts) >= e.idle_timeout_min * 60):
                out.append(e)
        out.sort(key=lambda e: e.last_used_ts)
        return [e.name for e in out]

    # ── R3 压力安全网 ──────────────────────────────────────────

    def pressure_eviction_plan(self, pressure_level: int) -> list[str]:
        """macOS 内存压力 ≥3 时给出休眠顺序：on_demand 先（最久未用优先），
        不足再动 resident。level<3 返回空。"""
        if pressure_level < 3:
            return []
        on_demand = [e for e in self._entries.values()
                     if e.mode is Mode.ON_DEMAND and e.state is Lifecycle.READY]
        on_demand.sort(key=lambda e: e.last_used_ts)
        residents = [e for e in self._entries.values()
                     if e.mode is Mode.RESIDENT and e.state is Lifecycle.READY]
        residents.sort(key=lambda e: e.last_used_ts)
        return [e.name for e in on_demand] + [e.name for e in residents]
