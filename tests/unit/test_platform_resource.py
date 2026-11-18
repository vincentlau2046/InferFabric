"""ResourceLedger interface + unified-memory backend (F2, spec §7).

Core scenarios re-run from the platform path. The legacy memory_ledger
shim-compat cases from the source fork are intentionally not carried
here — upstream has no shim.
"""
from inferfabric.platform.resource import (
    AdmitDecision,
    Lifecycle,
    Mode,
)
from inferfabric.platform.resource_umem import UnifiedMemoryLedger


def _ledger(budget=1000, probe=None):
    return UnifiedMemoryLedger(budget_mb=budget, probe_available_mb=probe)


def test_r1_admit_rejects_over_budget():
    led = _ledger(budget=1000)
    led.register("big", 1200, Mode.ON_DEMAND)
    d = led.admit("big")
    assert isinstance(d, AdmitDecision) and not d.ok
    assert "内存不足" in d.reason and "1200" in d.reason


def test_r1_admit_force_oversell_returns_ownership_to_human():
    led = _ledger(budget=1000)
    led.register("big", 1200, Mode.ON_DEMAND)
    d = led.admit("big", force=True)
    assert d.ok and "超卖" in d.reason


def test_r2_idle_sleep_candidates_on_demand_only():
    led = _ledger(budget=1000)
    od = led.register("od", 100, Mode.ON_DEMAND, idle_timeout_min=10)
    led.register("res", 100, Mode.RESIDENT)
    od.state = Lifecycle.READY
    od.last_used_ts -= 11 * 60
    assert led.idle_sleep_candidates() == ["od"]


def test_r3_pressure_evicts_on_demand_first_then_resident():
    led = _ledger(budget=1000)
    od = led.register("od", 100, Mode.ON_DEMAND)
    res = led.register("res", 100, Mode.RESIDENT)
    od.state = Lifecycle.READY
    res.state = Lifecycle.READY
    od.last_used_ts -= 100
    assert led.pressure_eviction_plan(3) == ["od", "res"]
    assert led.pressure_eviction_plan(2) == []


def test_probe_unavailable_falls_back_to_bookkeeping():
    """probe_available_mb=None → R1 books on baseline only, no exception."""
    led = _ledger(budget=500, probe=lambda: None)
    led.register("m", 400, Mode.ON_DEMAND)
    assert led.admit("m").ok


def test_resource_backend_selection_on_linux(monkeypatch):
    """Review Focus 1: forced linux profile selects the vram backend name."""
    monkeypatch.setenv("IFF_FORCE_PLATFORM", "linux")
    from inferfabric.platform import detect
    assert detect().resource_backend == "vram"
