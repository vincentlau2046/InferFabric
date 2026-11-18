"""platform.apple_telemetry — Apple Silicon telemetry backend.

Reads only through platform.probe (sysctl/vm_stat/pressure = single source
of truth). Per spec §7.2 + §14 rulings:

- memory_kind="unified"; total = physical RAM, budget = B = RAM − reserve.
- used and budget share ONE hw.memsize read per snapshot (no inconsistent
  windows).
- budget is None when total unknown — never the ledger's internal 0.
- power/temperature/clock/utilization unsupported until a verified
  unprivileged source exists (§14.4: fail-open None, never fabricated).
- unified-memory pressure is NEVER mapped to GPU utilization (anti-goal).
"""
from __future__ import annotations

import time

from . import probe
from .telemetry import DeviceCapabilities, DeviceSnapshot

__all__ = ["AppleSiliconTelemetry"]

_SOURCES_UNSUPPORTED: dict[str, str] = {}


class AppleSiliconTelemetry:
    """Read-only unified-memory telemetry for darwin profiles."""

    def __init__(self, *, clock=time.time):
        self._clock = clock

    def snapshot(self) -> DeviceSnapshot:
        total = probe.mem_total_mb()
        used = probe.mem_used_given_total(total)     # shares the total read
        budget = probe.budget_given_total(total)
        pressure = probe.memory_pressure_level()
        sources: dict[str, str] = {}
        if total is not None:
            sources["memory_total_mb"] = "sysctl"
        if used is not None:
            sources["memory_used_mb"] = "vm_stat"
        if budget is not None:
            sources["memory_budget_mb"] = "probe"
        if pressure is not None:
            sources["pressure_level"] = "sysctl"
        return DeviceSnapshot(
            platform="darwin",
            accelerator="apple_silicon",
            memory_kind="unified",
            memory_used_mb=used,
            memory_total_mb=total,
            memory_budget_mb=budget,
            utilization_pct=None,        # no verified unprivileged source
            power_w=None,
            temperature_c=None,
            clock_mhz=None,
            pressure_level=pressure,
            timestamp=self._clock(),
            sources=sources,
        )

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(memory=True, pressure=True)
