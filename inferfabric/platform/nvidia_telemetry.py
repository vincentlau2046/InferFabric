"""platform.nvidia_telemetry — NVIDIA telemetry + device control backend.

Spec §7.1 + §14 rulings: nvidia-smi only this phase (no NVML), scalar
aggregate across cards (v1), per-field availability (complete-column rule),
observation failures fail open to None, reset is DeviceControl-only.

Complete-column rule: a metric is published only when EVERY returned card
reports a valid value for it; one N/A drops the whole column (never a
partial sum posing as the fleet total). Memory used/total and power are
summed; utilization/temperature are averaged; clock is averaged and rounded
to int. memory_budget_mb = int(total × 0.97) — the existing VRAM guard.
"""
from __future__ import annotations

import math
import subprocess
import time
from typing import Callable

from .telemetry import DeviceCapabilities, DeviceSnapshot, OperationResult

__all__ = ["NvidiaTelemetry", "NvidiaDeviceControl",
           "parse_nvidia_snapshot", "RESET_IDLE_THRESHOLD_MB"]

QUERY_CMD = [
    "nvidia-smi",
    "--query-gpu=memory.used,memory.total,utilization.gpu,power.draw,"
    "temperature.gpu,clocks.sm",
    "--format=csv,noheader,nounits",
]

# Per-card memory used at or below this = idle for reset purposes
# (conservative platform-layer constant; gateway config stays out of here).
RESET_IDLE_THRESHOLD_MB = 1024

_COLUMNS = ("memory_used_mb", "memory_total_mb", "utilization_pct",
            "power_w", "temperature_c", "clock_mhz")

# (low, high) inclusive validity windows; anything else invalid
_RANGES = {
    "memory_used_mb": (0, 10**7),
    "memory_total_mb": (0, 10**7),
    "utilization_pct": (0.0, 100.0),
    "power_w": (0.0, 5000.0),
    "temperature_c": (-100.0, 150.0),
    "clock_mhz": (0, 10**6),
}


def _valid(name: str, v: float) -> bool:
    lo, hi = _RANGES[name]
    return math.isfinite(v) and lo <= v <= hi


def parse_nvidia_snapshot(stdout: str, *, timestamp: float) -> DeviceSnapshot:
    """Pure parser: nvidia-smi CSV → DeviceSnapshot (no I/O, no clock)."""
    rows = [line.strip() for line in stdout.splitlines() if line.strip()]
    n = len(rows)
    values: dict[str, list[float]] = {}
    for row in rows:
        cells = [c.strip() for c in row.split(",")]
        for i, name in enumerate(_COLUMNS):
            if i >= len(cells):
                continue
            try:
                v = float(cells[i])
            except ValueError:
                continue  # N/A / malformed → invalid for this card
            if _valid(name, v):
                values.setdefault(name, []).append(v)

    def complete(name: str) -> list[float] | None:
        got = values.get(name, [])
        return got if n > 0 and len(got) == n else None

    used_col = complete("memory_used_mb")
    total_col = complete("memory_total_mb")
    util_col = complete("utilization_pct")
    power_col = complete("power_w")
    temp_col = complete("temperature_c")
    clock_col = complete("clock_mhz")

    memory_used = int(sum(used_col)) if used_col else None
    memory_total = int(sum(total_col)) if total_col else None
    memory_budget = int(memory_total * 0.97) if memory_total is not None else None
    utilization = round(sum(util_col) / len(util_col), 1) if util_col else None
    power = round(sum(power_col), 1) if power_col else None
    temperature = round(sum(temp_col) / len(temp_col), 1) if temp_col else None
    clock = int(round(sum(clock_col) / len(clock_col))) if clock_col else None

    sources = {name: "nvidia-smi" for name in
               ("memory_used_mb", "memory_total_mb", "memory_budget_mb",
                "utilization_pct", "power_w", "temperature_c", "clock_mhz")
               if name in ("memory_used_mb", "memory_total_mb",
                           "utilization_pct", "power_w", "temperature_c",
                           "clock_mhz")
               and (used_col if name == "memory_used_mb" else
                    total_col if name == "memory_total_mb" else
                    util_col if name == "utilization_pct" else
                    power_col if name == "power_w" else
                    temp_col if name == "temperature_c" else clock_col)}

    return DeviceSnapshot(
        platform="linux", accelerator="nvidia", memory_kind="vram",
        memory_used_mb=memory_used, memory_total_mb=memory_total,
        memory_budget_mb=memory_budget, utilization_pct=utilization,
        power_w=power, temperature_c=temperature, clock_mhz=clock,
        pressure_level=None, timestamp=timestamp, sources=sources,
    )


class NvidiaTelemetry:
    """nvidia-smi backed telemetry; never raises to the caller."""

    def __init__(self, *, run: Callable | None = None, clock=time.time,
                 timeout: float = 5.0):
        self._run = run if run is not None else subprocess.run
        self._clock = clock
        self._timeout = timeout

    def snapshot(self) -> DeviceSnapshot:
        try:
            r = self._run(QUERY_CMD, capture_output=True, text=True,
                          timeout=self._timeout)
            if r.returncode == 0 and r.stdout.strip():
                return parse_nvidia_snapshot(r.stdout, timestamp=self._clock())
        except Exception:
            pass
        # Command missing / failed / timed out → unavailable observations
        return DeviceSnapshot(
            platform="linux", accelerator="nvidia", memory_kind="vram",
            memory_used_mb=None, memory_total_mb=None, memory_budget_mb=None,
            utilization_pct=None, power_w=None, temperature_c=None,
            clock_mhz=None, pressure_level=None, timestamp=self._clock(),
            sources={},
        )

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            memory=True, utilization=True, power=True, temperature=True,
            clock=True, pressure=False, reset=True)


class NvidiaDeviceControl:
    """GPU reset behind DeviceControl (relocated from process_manager).

    Safety gates before any write command:
      1. Occupancy probe must succeed — unknown → refuse (no commands).
      2. Target card must be idle for the scoped attempt.
      3. Unscoped fallbacks (pm toggle, -r) require ALL cards idle — a
         scoped request never widens scope onto a busy card.
    """

    def __init__(self, *, gpu_index: int = 0, run: Callable | None = None):
        self._gpu_index = gpu_index
        self._run = run if run is not None else subprocess.run

    def _per_card_used_mb(self) -> list[int] | None:
        try:
            r = self._run(
                ["nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            out = []
            for line in r.stdout.strip().splitlines():
                v = int(line.strip())
                if v < 0:
                    return None
                out.append(v)
            return out or None
        except Exception:
            return None

    def reset(self) -> OperationResult:
        used = self._per_card_used_mb()
        if used is None:
            return OperationResult(
                ok=False, status="error",
                message="device occupancy unknown — reset refused")
        if self._gpu_index >= len(used):
            return OperationResult(
                ok=False, status="error",
                message=f"gpu_index {self._gpu_index} out of range "
                        f"({len(used)} cards)")
        if used[self._gpu_index] > RESET_IDLE_THRESHOLD_MB:
            return OperationResult(
                ok=False, status="error",
                message=f"card {self._gpu_index} busy "
                        f"({used[self._gpu_index]} MB used) — reset refused")

        others_busy = [i for i, u in enumerate(used)
                       if i != self._gpu_index and u > RESET_IDLE_THRESHOLD_MB]

        # Scoped attempt: --gpu-reset -i <idx>
        try:
            r = self._run(["nvidia-smi", "--gpu-reset", "-i", str(self._gpu_index)],
                          capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return OperationResult(ok=True, status="ok",
                                       message="gpu-reset")
        except Exception:
            pass

        # Unscoped fallbacks need every other card idle too
        if others_busy:
            return OperationResult(
                ok=False, status="error",
                message=f"other cards busy {others_busy} — refusing to "
                        f"widen reset scope")

        try:
            r = self._run(["nvidia-smi", "-pm", "0"],
                          capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                time.sleep(1)
                self._run(["nvidia-smi", "-pm", "1"],
                          capture_output=True, text=True, timeout=10)
                time.sleep(1)
                return OperationResult(ok=True, status="ok",
                                       message="pm-toggle")
        except Exception:
            pass

        try:
            r = self._run(["nvidia-smi", "-r"],
                          capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return OperationResult(ok=True, status="ok",
                                       message="nvidia-smi-r")
        except Exception:
            pass

        return OperationResult(
            ok=False, status="error",
            message="no GPU reset method succeeded")
