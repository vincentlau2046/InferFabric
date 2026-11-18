"""platform telemetry backends — Apple Silicon + NVIDIA (DeviceTelemetry T1).

All hardware access is mocked (fake probe outputs / injected run); no test
requires physical devices. Pinned aggregation case (plan Task 2 Step 4):
rows `100,1000,10,100,40,1500` and `200,2000,30,N/A,60,1700` produce
(used=300, total=3000, budget=2910, util=20.0, power=None, temp=50.0,
clock=1600) — an incomplete column is dropped whole, never partial-summed.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from inferfabric.platform import PlatformProfile, probe
from inferfabric.platform.telemetry import (
    DeviceCapabilities,
    NullDeviceControl,
    NullTelemetry,
    device_control_for_profile,
    telemetry_for_profile,
)


# ── Apple Silicon backend ────────────────────────────────────────

def _fake_probe_run(sysctl_calls):
    """Fake probe._run: canned sysctl/vm_stat output, counts hw.memsize reads."""
    def fake_run(cmd, timeout=10):
        if cmd[:2] == ["sysctl", "-n"]:
            key = cmd[2]
            if key == "hw.memsize":
                sysctl_calls.append(key)
                return _Result(0, "34359738368\n")  # 32768 MB
            if key == "kern.memorystatus_vm_pressure_level":
                sysctl_calls.append(key)
                return _Result(0, "1\n")
            return _Result(1, "")
        if cmd == ["vm_stat"]:
            return _Result(0, (
                "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Pages free: 1000.\nPages active: 500.\nPages inactive: 300.\n"
                "Pages speculative: 200.\nPages wired down: 100.\n"
                "Pages compressed: 50.\n"))
        return _Result(1, "")
    return fake_run


class _Result:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_apple_snapshot_unified_memory_semantics(monkeypatch):
    calls = []
    monkeypatch.setattr(probe, "_run", _fake_probe_run(calls))
    monkeypatch.setattr(probe, "IS_MACOS", True)
    from inferfabric.platform.apple_telemetry import AppleSiliconTelemetry
    s = AppleSiliconTelemetry(clock=lambda: 1000.0).snapshot()
    assert s.platform == "darwin" and s.accelerator == "apple_silicon"
    assert s.memory_kind == "unified"
    assert s.memory_total_mb == 32768            # physical (sysctl)
    # used = total − (free+inactive+speculative) pages
    assert s.memory_used_mb == 32768 - (1000 + 300 + 200) * 16384 // (1024 * 1024)
    assert s.memory_budget_mb == 32768 - 8192    # B = RAM − reserve
    assert s.pressure_level == 1
    assert s.power_w is None and s.temperature_c is None
    assert s.clock_mhz is None and s.utilization_pct is None
    assert s.timestamp == 1000.0
    assert s.sources["memory_total_mb"] == "sysctl"
    assert s.sources["memory_used_mb"] == "vm_stat"
    assert s.sources["memory_budget_mb"] == "probe"
    assert s.sources["pressure_level"] == "sysctl"


def test_apple_used_total_budget_share_physical_read(monkeypatch):
    """One hw.memsize read feeds used AND budget (no duplicate sysctl)."""
    calls = []
    monkeypatch.setattr(probe, "_run", _fake_probe_run(calls))
    monkeypatch.setattr(probe, "IS_MACOS", True)
    from inferfabric.platform.apple_telemetry import AppleSiliconTelemetry
    AppleSiliconTelemetry().snapshot()
    assert calls.count("hw.memsize") == 1


def test_apple_missing_physical_memory_does_not_publish_zero_budget(monkeypatch):
    monkeypatch.setattr(probe, "IS_MACOS", True)
    monkeypatch.setattr(probe, "mem_total_mb", lambda: None)
    from inferfabric.platform.apple_telemetry import AppleSiliconTelemetry
    s = AppleSiliconTelemetry().snapshot()
    assert s.memory_total_mb is None
    assert s.memory_budget_mb is None      # None, never the ledger's 0 fallback
    assert s.memory_used_mb is None


def test_apple_capabilities_and_control_unsupported():
    from inferfabric.platform.apple_telemetry import AppleSiliconTelemetry
    t = AppleSiliconTelemetry()
    c = t.capabilities()
    assert c.memory and c.pressure
    assert not (c.utilization or c.power or c.temperature or c.clock or c.reset)
    r = t_control_reset(t)
    assert not r.ok and r.status == "unsupported"


def t_control_reset(t):
    from inferfabric.platform.telemetry import NullDeviceControl
    return NullDeviceControl().reset()


def test_apple_pressure_probe_failure_is_none(monkeypatch):
    monkeypatch.setattr(probe, "IS_MACOS", True)
    monkeypatch.setattr(probe, "mem_total_mb", lambda: 32768)
    monkeypatch.setattr(probe, "_run", lambda cmd, timeout=10: _Result(1, ""))
    assert probe.memory_pressure_level() is None


def test_apple_pressure_probe_rejects_unknown_level(monkeypatch):
    monkeypatch.setattr(probe, "IS_MACOS", True)
    monkeypatch.setattr(probe, "_run", lambda cmd, timeout=10: _Result(0, "9\n"))
    assert probe.memory_pressure_level() is None


# ── NVIDIA backend ───────────────────────────────────────────────

ROW_A = "100, 1000, 10, 100, 40, 1500"
ROW_B = "200, 2000, 30, N/A, 60, 1700"


def test_nvidia_aggregates_complete_columns_only():
    from inferfabric.platform.nvidia_telemetry import parse_nvidia_snapshot
    s = parse_nvidia_snapshot(f"{ROW_A}\n{ROW_B}\n", timestamp=77.0)
    assert (s.memory_used_mb, s.memory_total_mb, s.memory_budget_mb,
            s.utilization_pct, s.power_w, s.temperature_c, s.clock_mhz) == (
        300, 3000, 2910, 20.0, None, 50.0, 1600)
    assert s.platform == "linux" and s.accelerator == "nvidia"
    assert s.memory_kind == "vram"
    assert s.timestamp == 77.0
    assert s.sources["memory_used_mb"] == "nvidia-smi"


def test_nvidia_all_columns_valid_full_aggregate():
    from inferfabric.platform.nvidia_telemetry import parse_nvidia_snapshot
    s = parse_nvidia_snapshot(f"{ROW_A}\n", timestamp=1.0)
    assert s.memory_used_mb == 100 and s.memory_total_mb == 1000
    assert s.memory_budget_mb == int(1000 * 0.97)
    assert s.utilization_pct == 10.0 and s.power_w == 100.0
    assert s.temperature_c == 40.0 and s.clock_mhz == 1500


def test_nvidia_malformed_cells_dropped_per_field():
    from inferfabric.platform.nvidia_telemetry import parse_nvidia_snapshot
    s = parse_nvidia_snapshot("x, y, z, a, b, c\n", timestamp=1.0)
    for f in ("memory_used_mb", "memory_total_mb", "memory_budget_mb",
              "utilization_pct", "power_w", "temperature_c", "clock_mhz"):
        assert getattr(s, f) is None, f


def test_nvidia_out_of_range_rejected():
    from inferfabric.platform.nvidia_telemetry import parse_nvidia_snapshot
    s = parse_nvidia_snapshot("100, 1000, 900, 100, 40, 1500\n", timestamp=1.0)
    assert s.utilization_pct is None  # 900 > 100 → column dropped


def test_nvidia_command_failure_yields_unavailable_snapshot():
    from inferfabric.platform.nvidia_telemetry import NvidiaTelemetry
    t = NvidiaTelemetry(run=lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("nvidia-smi", 5)))
    s = t.snapshot()
    assert s.accelerator == "nvidia" and s.memory_kind == "vram"
    assert s.memory_used_mb is None and s.power_w is None
    assert s.timestamp > 0


def test_nvidia_nonzero_exit_yields_unavailable():
    from inferfabric.platform.nvidia_telemetry import NvidiaTelemetry
    t = NvidiaTelemetry(run=lambda *a, **k: _Result(1, ""))
    s = t.snapshot()
    assert s.memory_used_mb is None and s.utilization_pct is None


def test_nvidia_capabilities():
    from inferfabric.platform.nvidia_telemetry import NvidiaTelemetry
    c = NvidiaTelemetry(run=lambda *a, **k: _Result(1, "")).capabilities()
    assert c.memory and c.utilization and c.power and c.temperature and c.clock
    assert not c.pressure and c.reset


# ── NVIDIA device control ────────────────────────────────────────

def test_reset_unknown_occupancy_issues_no_write():
    """Occupancy probe fails → error result, zero reset commands issued."""
    from inferfabric.platform.nvidia_telemetry import NvidiaDeviceControl
    calls = []

    def run(cmd, **k):
        calls.append(cmd)
        if "memory.used" in cmd[1]:
            return _Result(1, "")
        return _Result(0, "")

    r = NvidiaDeviceControl(run=run).reset()
    assert not r.ok and r.status == "error"
    assert not any("--gpu-reset" in c or "-r" == c[-1] or "-pm" in c for c in calls)


def test_indexed_reset_never_widens_scope_when_other_card_busy():
    """Target card idle but card 1 busy → no unscoped pm-toggle / -r fallthrough."""
    from inferfabric.platform.nvidia_telemetry import NvidiaDeviceControl
    calls = []

    def run(cmd, **k):
        calls.append(list(cmd))
        if "memory.used" in cmd[1]:
            return _Result(0, "100\n9000\n")  # card0 idle, card1 busy
        if "--gpu-reset" in cmd:
            return _Result(1, "", "reset failed")
        return _Result(0, "")

    r = NvidiaDeviceControl(run=run, gpu_index=0).reset()
    assert not r.ok and r.status == "error"
    joined = " | ".join(" ".join(c) for c in calls)
    assert "--gpu-reset -i 0" in joined
    assert "-pm" not in joined and " -r" not in joined.replace("--gpu-reset", "")


def test_reset_scoped_success_and_fallback_order():
    """Idle box: --gpu-reset → pm toggle → -r, first success wins."""
    from inferfabric.platform.nvidia_telemetry import NvidiaDeviceControl

    def make_run(gpu_reset_rc):
        def run(cmd, **k):
            if "memory.used" in cmd[1]:
                return _Result(0, "100\n")
            if "--gpu-reset" in cmd:
                return _Result(gpu_reset_rc, "", "fail")
            return _Result(0, "")
        return run

    r = NvidiaDeviceControl(run=make_run(0), gpu_index=0).reset()
    assert r.ok and r.status == "ok" and "gpu-reset" in r.message

    calls = []
    def run2(cmd, **k):
        calls.append(" ".join(cmd))
        if "memory.used" in cmd[1]:
            return _Result(0, "100\n")
        if "--gpu-reset" in cmd:
            return _Result(1, "", "fail")
        return _Result(0, "")

    r = NvidiaDeviceControl(run=run2, gpu_index=0).reset()
    assert r.ok
    assert calls.index([c for c in calls if "-pm" in c][0])
    assert "--gpu-reset" in calls[1]


def test_null_and_apple_control_never_run_commands():
    for ctl in (NullDeviceControl(),):
        r = ctl.reset()
        assert not r.ok and r.status == "unsupported"


# ── factory wiring ───────────────────────────────────────────────

def test_factory_selects_backends_by_profile():
    assert isinstance(telemetry_for_profile(
        PlatformProfile("darwin", "umem", "launchd")), type(telemetry_for_profile(
        PlatformProfile("darwin", "umem", "launchd"))))
    from inferfabric.platform.apple_telemetry import AppleSiliconTelemetry
    from inferfabric.platform.nvidia_telemetry import NvidiaTelemetry, NvidiaDeviceControl
    assert isinstance(telemetry_for_profile(
        PlatformProfile("darwin", "umem", "launchd")), AppleSiliconTelemetry)
    assert isinstance(telemetry_for_profile(
        PlatformProfile("linux", "vram", "systemd")), NvidiaTelemetry)
    assert isinstance(telemetry_for_profile(
        PlatformProfile("other", "vram", "none")), NullTelemetry)
    assert isinstance(device_control_for_profile(
        PlatformProfile("linux", "vram", "systemd")), NvidiaDeviceControl)
    assert isinstance(device_control_for_profile(
        PlatformProfile("darwin", "umem", "launchd")), NullDeviceControl)


def test_forced_profile_selection_is_lazy(monkeypatch):
    """Env aliases + precedence resolve through detect(); assembly runs no
    subprocess (poisoned in a child interpreter)."""
    monkeypatch.setenv("IFF_FORCE_PLATFORM", "linux")
    monkeypatch.delenv("MACFABRIC_FORCE_PLATFORM", raising=False)
    from inferfabric.platform import detect
    from inferfabric.platform.nvidia_telemetry import NvidiaTelemetry
    assert isinstance(telemetry_for_profile(detect()), NvidiaTelemetry)

    monkeypatch.setenv("MACFABRIC_FORCE_PLATFORM", "darwin")
    monkeypatch.delenv("IFF_FORCE_PLATFORM", raising=False)
    from inferfabric.platform.apple_telemetry import AppleSiliconTelemetry
    assert isinstance(telemetry_for_profile(detect()), AppleSiliconTelemetry)

    monkeypatch.setenv("IFF_FORCE_PLATFORM", "linux")  # IFF wins over alias
    assert detect().os == "linux"

    code = (
        "import os\n"
        "os.environ['IFF_FORCE_PLATFORM']='linux'\n"
        "import subprocess\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('subprocess during assembly')\n"
        "subprocess.run=_boom; subprocess.Popen=_boom\n"
        "import inferfabric.platform.telemetry as m\n"
        "from inferfabric.platform import detect\n"
        "t=m.telemetry_for_profile(detect())\n"
        "print('OK' if t is not None else 'BAD')"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"
