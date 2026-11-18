"""DeviceTelemetry business wiring — Task 3 (health/admission/control/CLI).

Fake telemetry drives every path; no real device commands run here.
Fail-closed contract: missing measurements never become 0, a pass, or a
fabricated total on admission/control paths; observations stay None.
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inferfabric.platform.telemetry import (
    DeviceCapabilities,
    DeviceSnapshot,
    NullDeviceControl,
    OperationResult,
)
from inferfabric.platform import PlatformProfile


def _snap(**over):
    fields = dict(
        platform="darwin", accelerator="apple_silicon", memory_kind="unified",
        memory_used_mb=8000, memory_total_mb=32768, memory_budget_mb=24576,
        utilization_pct=None, power_w=None, temperature_c=None,
        clock_mhz=None, pressure_level=1, timestamp=100.0,
        sources={"memory_used_mb": "vm_stat"},
    )
    fields.update(over)
    return DeviceSnapshot(**fields)


class FakeTelemetry:
    def __init__(self, snapshot, calls=None):
        self._s = snapshot
        self.calls = calls if calls is not None else []

    def snapshot(self):
        self.calls.append("snapshot")
        return self._s

    def capabilities(self):
        return DeviceCapabilities(memory=True, pressure=True)


class FakeControl:
    def __init__(self, result):
        self.result = result
        self.called = 0

    def reset(self):
        self.called += 1
        return self.result


# ── health helpers ───────────────────────────────────────────────

def test_gpu_used_delegates_and_none_passes_through():
    from inferfabric import health
    assert health.gpu_used_mb(FakeTelemetry(_snap(memory_used_mb=123))) == 123
    assert health.gpu_used_mb(FakeTelemetry(_snap(memory_used_mb=None))) is None


def test_gpu_total_apple_maps_budget_not_physical():
    from inferfabric import health
    # unified: budget B (24576), NOT physical 32768 (spec §14.2)
    assert health.gpu_total_mb(FakeTelemetry(_snap())) == 24576


def test_gpu_total_nvidia_maps_physical_vram():
    from inferfabric import health
    snap = _snap(platform="linux", accelerator="nvidia", memory_kind="vram",
                 memory_used_mb=300, memory_total_mb=3000,
                 memory_budget_mb=2910, utilization_pct=20.0)
    assert health.gpu_total_mb(FakeTelemetry(snap)) == 3000
    assert health.gpu_used_mb(FakeTelemetry(snap)) == 300


def test_wait_gpu_free_fails_closed_on_unavailable():
    from inferfabric import health
    # None reading must never be treated as 0 (free); timeout → False.
    assert health.wait_gpu_free(timeout=0, telemetry=FakeTelemetry(
        _snap(memory_used_mb=None))) is False


def test_wait_gpu_free_true_when_below_threshold():
    from inferfabric import health
    assert health.wait_gpu_free(timeout=1, threshold_mb=512,
                                telemetry=FakeTelemetry(
                                    _snap(memory_used_mb=100))) is True


# ── VRAM guards (extracted check) ────────────────────────────────

def _model(peak_vram_mb=4000, typical_vram_pct=0, gpu_role="cuda"):
    m = type("M", (), {})
    m.peak_vram_mb = peak_vram_mb
    m.typical_vram_pct = typical_vram_pct
    m.gpu_role = gpu_role
    m.name = "m1"
    return m


def test_peak_guard_passes_within_budget():
    from inferfabric.model_lifecycle import check_vram_budget
    snap = _snap(platform="linux", accelerator="nvidia", memory_kind="vram",
                 memory_used_mb=3000, memory_total_mb=10000,
                 memory_budget_mb=9700)
    assert check_vram_budget(_model(peak_vram_mb=4000),
                             FakeTelemetry(snap)) is None


def test_peak_guard_rejects_over_budget():
    from inferfabric.model_lifecycle import check_vram_budget
    snap = _snap(platform="linux", accelerator="nvidia", memory_kind="vram",
                 memory_used_mb=6000, memory_total_mb=10000,
                 memory_budget_mb=9700)
    d = check_vram_budget(_model(peak_vram_mb=4000), FakeTelemetry(snap))
    assert d is not None and d["status"] == "error"
    assert "VRAM budget exceeded" in d["message"]


def test_peak_guard_fails_closed_when_telemetry_unavailable():
    from inferfabric.model_lifecycle import check_vram_budget
    snap = _snap(platform="linux", accelerator="nvidia", memory_kind="vram",
                 memory_used_mb=None, memory_total_mb=None,
                 memory_budget_mb=None)
    d = check_vram_budget(_model(peak_vram_mb=4000), FakeTelemetry(snap))
    assert d is not None and d["status"] == "error"
    assert "unavailable" in d["message"].lower()


def test_pct_guard_fails_closed_when_unavailable():
    from inferfabric.model_lifecycle import check_vram_budget
    snap = _snap(platform="linux", accelerator="nvidia", memory_kind="vram",
                 memory_used_mb=None, memory_total_mb=None)
    d = check_vram_budget(_model(peak_vram_mb=0, typical_vram_pct=30),
                          FakeTelemetry(snap))
    assert d is not None and d["status"] == "error"


# ── manager status: device + legacy fields from ONE snapshot ─────

def _make_manager(tmp_path, snapshot):
    from inferfabric.manager import ModelManager
    from inferfabric.state import StateDB
    snap_calls = []
    tel = FakeTelemetry(snapshot, snap_calls)
    mgr = ModelManager(
        models_dir=tmp_path / "models.d",
        state_db_path=tmp_path / "state.db",
        device_telemetry=tel,
    )
    (tmp_path / "models.d").mkdir(exist_ok=True)
    return mgr, tel, snap_calls


def test_manager_status_emits_device_and_legacy_fields(tmp_path):
    (tmp_path / "models.d").mkdir()
    mgr, tel, calls = _make_manager(tmp_path, _snap())
    s = mgr.status()
    assert "device" in s and s["device"]["snapshot"]["memory_kind"] == "unified"
    assert s["gpu_used_mb"] == 8000
    assert s["gpu_total_mb"] == 24576           # Apple → budget
    assert s["gpu_util_pct"] is None            # Apple: no fabricated pct
    # one snapshot feeds status (plus None-default assembly at construction)
    assert calls


def test_manager_status_nvidia_legacy_utilization(tmp_path):
    (tmp_path / "models.d").mkdir()
    snap = _snap(platform="linux", accelerator="nvidia", memory_kind="vram",
                 memory_used_mb=300, memory_total_mb=3000,
                 memory_budget_mb=2910, utilization_pct=20.0)
    mgr, _, _ = _make_manager(tmp_path, snap)
    s = mgr.status()
    assert s["gpu_total_mb"] == 3000
    assert abs(s["gpu_util_pct"] - 10.0) < 0.01  # legacy occupancy = used/total


# ── process manager: wait + reset wiring ─────────────────────────

def _base_proc(tmp_path):
    from inferfabric.process_manager.base import BaseProcessManager
    from inferfabric.state import StateDB
    state = StateDB(tmp_path / "state.db")
    return BaseProcessManager(state, tmp_path / "logs")


def test_wait_gpu_idle_unavailable_never_reports_free(tmp_path, monkeypatch):
    from inferfabric.process_manager import base as pmb
    proc = _base_proc(tmp_path)
    monkeypatch.setattr(pmb, "gpu_used_mb",
                        lambda telemetry=None: None)
    r = proc._wait_gpu_idle(timeout=1)
    assert r["status"] != "ok"


def test_clear_gpu_cuda_state_routes_through_device_control(tmp_path):
    from inferfabric.process_manager.facade import ProcessManager
    from inferfabric.state import StateDB
    ctrl = FakeControl(OperationResult(ok=True, status="ok",
                                       message="gpu-reset"))
    proc = ProcessManager(StateDB(tmp_path / "state.db"),
                          device_control=ctrl)
    r = proc.clear_gpu_cuda_state(force=True)
    assert ctrl.called == 1
    assert r["status"] == "ok" and r["method"] == "gpu-reset"


def test_clear_gpu_cuda_state_unsupported_maps_error(tmp_path):
    from inferfabric.process_manager.facade import ProcessManager
    from inferfabric.state import StateDB
    proc = ProcessManager(StateDB(tmp_path / "state.db"),
                          device_control=NullDeviceControl())
    r = proc.clear_gpu_cuda_state(force=True)
    assert r["status"] == "error" and "not supported" in r["message"].lower()


def test_force_reset_gpu_free_handles_none(tmp_path, monkeypatch):
    """gpu_state.force_reset must not crash on a None reading."""
    from inferfabric import gpu_state as gs
    (tmp_path / "models.d").mkdir()
    from inferfabric.manager import ModelManager
    from inferfabric.state import StateDB
    snap = _snap(memory_used_mb=None, memory_budget_mb=None)
    mgr = ModelManager(models_dir=tmp_path / "models.d",
                       state_db_path=tmp_path / "state.db",
                       device_telemetry=FakeTelemetry(snap))
    mgr.state.set_multi({"gpu_mode": "idle", "active_services": "[]"})
    # CRITICAL isolation (review): never let the state machine issue real
    # host-wide kills (pkill -f / fuser -k) or nvidia-smi resets from a unit
    # test — stub the process facade; only the telemetry contract is exercised.
    kills = []
    _stub = SimpleNamespace(
        force_kill_all=lambda: kills.append("kill"),
        clear_gpu_cuda_state=lambda force=False: kills.append("clear"),
    )
    mgr._proc = _stub
    mgr._gpu_state._proc = _stub  # state machine keeps its own reference
    # short-circuit the 20s wait loop; the None→False contract is asserted
    # directly at the wait_gpu_free level in test_wait_gpu_free_*
    import inferfabric.gpu_state as gs
    monkeypatch.setattr(gs, "wait_gpu_free", lambda *a, **kw: False)
    r = mgr._gpu_state.force_reset()
    assert kills == ["kill", "clear"]  # no real host commands ran
    assert r["gpu_free"] is False        # None ≠ free
    assert r["status"] == "reset"


def test_force_reset_gpu_free_reflects_wait_result(tmp_path, monkeypatch):
    """gpu_free must be the wait outcome, not the stale pre-wait sample.

    Sequence: first probe (logged used) is busy; by the wait loop the device
    is free → gpu_free True (old code reported False from the stale sample).
    """
    from inferfabric.manager import ModelManager
    seq = [_snap(memory_used_mb=30000),
           _snap(memory_used_mb=100),
           _snap(memory_used_mb=100)]

    class SeqTelemetry:
        def snapshot(self):
            return seq.pop(0) if seq else _snap(memory_used_mb=100)

        def capabilities(self):
            return DeviceCapabilities(memory=True)

    mgr = ModelManager(models_dir=tmp_path / "models.d",
                       state_db_path=tmp_path / "state.db",
                       device_telemetry=SeqTelemetry())
    mgr.state.set_multi({"gpu_mode": "idle", "active_services": "[]"})
    _stub = SimpleNamespace(
        force_kill_all=lambda: None,
        clear_gpu_cuda_state=lambda force=False: None,
    )
    mgr._proc = _stub
    mgr._gpu_state._proc = _stub
    r = mgr._gpu_state.force_reset()
    assert r["gpu_free"] is True


def test_force_kill_all_routes_clear_through_device_control(tmp_path, monkeypatch):
    """force_kill_all's post-kill clear must go through the gated
    DeviceControl path, not the legacy ungated nvidia-smi sequence."""
    from inferfabric.process_manager import facade as fmod
    from inferfabric.state import StateDB
    monkeypatch.setattr(fmod, "GPU_AUTO_CLEAR_CUDA_STATE", True)
    monkeypatch.setattr(fmod.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(fmod.os, "killpg", lambda *a, **kw: None)
    import inferfabric.process_manager.base as pmb
    monkeypatch.setattr(pmb, "gpu_used_mb", lambda telemetry=None: None)

    ctrl = FakeControl(OperationResult(ok=True, status="ok", message="gpu-reset"))
    proc = fmod.ProcessManager(StateDB(tmp_path / "state.db"),
                               device_control=ctrl)
    proc.force_kill_all()
    assert ctrl.called == 1
