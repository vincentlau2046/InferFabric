"""VRAM three-state ResourceLedger backend (CUDA / upstream InferFabric line).

Wraps upstream GPU-occupancy semantics as a ledger: accounting is bookkeeping
over VRAM MB; exclusivity is delegated to an injected `gpu` object with the
minimal protocol::

    gpu.would_block(name: str, needed_mb: int, exclusive: bool) -> bool

The gpu module is injected (constructor), never imported at module scope, so
this unit is importable and testable on any platform. Lifecycle/sleep/pressure
semantics are inherited from the unified-memory backend — on a VRAM card,
"sleep" means unload the model, which is the same bookkeeping.

User-facing reason strings are English (upstream dashboard style).
"""
from __future__ import annotations

from typing import Optional

from .resource import AdmitDecision, Lifecycle, Mode, ProbeCallable
from .resource_umem import UnifiedMemoryLedger


class VramThreeStateLedger(UnifiedMemoryLedger):
    """Budget semantics = VRAM bytes. R1 admission adds a GPU-mode gate."""

    def __init__(
        self,
        budget_mb: int,
        gpu=None,  # object with would_block(); e.g. upstream GpuStateMachine facade
        probe_available_mb: Optional[ProbeCallable] = None,
    ):
        super().__init__(budget_mb=budget_mb, probe_available_mb=probe_available_mb)
        self._gpu = gpu

    def admit(self, name: str, force: bool = False) -> AdmitDecision:
        e = self._entries.get(name)
        if e is None:
            return AdmitDecision(False, f"model {name} is not registered")
        if e.state in (Lifecycle.READY, Lifecycle.LOADING):
            return AdmitDecision(True, "already running")
        if e.state == Lifecycle.SLEEPING:
            return AdmitDecision(True, "sleeping; wake to serve")

        needed = e.mem_baseline_mb

        # Three-state gate: exclusive deployments must not collide with a
        # running exclusive/shared service (upstream safe-transition rule).
        if self._gpu is not None and self._gpu.would_block(name, needed, True):
            if not force:
                return AdmitDecision(
                    False,
                    f"GPU busy: stop the running model or free {needed}MB of VRAM"
                    f" before loading {name}",
                    self.budget_mb - self.resident_total_mb(), needed,
                )

        d = super().admit(name, force=force)
        if not d.ok:
            # Re-state the reason in upstream (English) dashboard style.
            return AdmitDecision(
                False,
                f"insufficient VRAM: need {d.needed_mb}MB, budget has {d.available_mb}MB"
                f" (B = {self.budget_mb}MB, reserved {self.resident_total_mb()}MB)",
                d.available_mb, d.needed_mb,
            )
        return d
