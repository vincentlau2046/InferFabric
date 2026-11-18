"""ResourceLedger — the resource-account interface every platform backend
implements (spec §7; behaviour contract = PLAN.md memory rules R1/R2/R3).

Two backends ship:
    resource_umem.py  UnifiedMemoryLedger — Apple Silicon unified memory
                      (B = RAM − reserve, pure bookkeeping, probes injected)
    resource_vram.py  VramThreeStateLedger — upstream CUDA three-state GPU
                      state machine, wrapped as a ledger

Gateway core (manager/proxy) must depend on these types only, never on a
concrete backend module.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Mode(str, Enum):
    RESIDENT = "resident"      # 常驻：只受 R1/R3 约束，无空闲休眠
    ON_DEMAND = "on_demand"    # 按需：空闲 idle_timeout_min 后可被 R2/R3 休眠


class Lifecycle(str, Enum):
    STOPPED = "stopped"
    LOADING = "loading"
    READY = "ready"
    SLEEPING = "sleeping"


@dataclass
class AdmitDecision:
    ok: bool
    reason: str = ""
    available_mb: int = 0
    needed_mb: int = 0


@dataclass
class LedgerEntry:
    name: str
    mem_baseline_mb: int
    mode: Mode
    idle_timeout_min: float = 30.0
    state: Lifecycle = Lifecycle.STOPPED
    last_used_ts: float = field(default_factory=time.time)

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_used_ts


class ResourceLedger(ABC):
    """Accounting + admission interface. Budget semantics are backend-owned
    (unified memory bytes vs VRAM bytes); rule semantics are interface-owned."""

    @abstractmethod
    def register(self, name: str, mem_baseline_mb: int, mode: Mode,
                 idle_timeout_min: float = 30.0) -> LedgerEntry: ...

    @abstractmethod
    def admit(self, name: str, force: bool = False) -> AdmitDecision: ...

    @abstractmethod
    def resident_total_mb(self) -> int: ...

    @abstractmethod
    def mark_ready(self, name: str) -> None: ...

    @abstractmethod
    def mark_sleeping(self, name: str) -> None: ...

    @abstractmethod
    def mark_stopped(self, name: str) -> None: ...

    @abstractmethod
    def touch(self, name: str) -> None: ...

    @abstractmethod
    def idle_sleep_candidates(self, now: Optional[float] = None) -> list[str]: ...

    @abstractmethod
    def pressure_eviction_plan(self, pressure_level: int) -> list[str]: ...

    @abstractmethod
    def registered(self, name: str) -> Optional[LedgerEntry]: ...

    @abstractmethod
    def entries(self) -> dict[str, LedgerEntry]: ...


# Re-exported for callers that only want the probe contract shape.
ProbeCallable = Callable[[], Optional[int]]
