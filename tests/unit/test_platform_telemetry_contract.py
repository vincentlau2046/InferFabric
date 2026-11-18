"""platform.telemetry — unified device telemetry contract (DeviceTelemetry T0).

Contract freeze: dataclasses, protocols, Null backends, factories, and
JSON serialization. Platform command behavior belongs to Task 2; this file
must pass without any subprocess execution.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inferfabric.platform import PlatformProfile
from inferfabric.platform.telemetry import (
    DeviceCapabilities,
    DeviceSnapshot,
    NullDeviceControl,
    NullTelemetry,
    OperationResult,
    capabilities_to_dict,
    device_control_for_profile,
    snapshot_to_dict,
    telemetry_for_profile,
)


def _snap(**over):
    fields = dict(
        platform="other", accelerator="unknown", memory_kind="unknown",
        memory_used_mb=None, memory_total_mb=None, memory_budget_mb=None,
        utilization_pct=None, power_w=None, temperature_c=None,
        clock_mhz=None, pressure_level=None, timestamp=1234.5, sources={},
    )
    fields.update(over)
    return DeviceSnapshot(**fields)


# ── dataclass shapes ─────────────────────────────────────────────

def test_device_snapshot_fields_and_none_ready():
    s = _snap(memory_used_mb=100)
    assert s.platform == "other" and s.accelerator == "unknown"
    assert s.memory_kind == "unknown"
    assert s.memory_used_mb == 100
    for f in ("memory_total_mb", "memory_budget_mb", "utilization_pct",
              "power_w", "temperature_c", "clock_mhz", "pressure_level"):
        assert getattr(s, f) is None, f
    assert isinstance(s.timestamp, float) and s.sources == {}


def test_device_capabilities_static_shape():
    c = DeviceCapabilities(
        memory=True, utilization=False, power=False, temperature=False,
        clock=False, pressure=False, reset=False)
    assert c.memory and not c.reset


def test_operation_result_unsupported():
    r = OperationResult(ok=False, status="unsupported",
                        message="reset not supported", details=None)
    assert not r.ok and r.status == "unsupported"


# ── serialization ────────────────────────────────────────────────

def test_snapshot_to_dict_is_json_null_ready():
    d = snapshot_to_dict(_snap(memory_used_mb=100, sources={"memory_used_mb": "probe"}))
    assert d["memory_used_mb"] == 100
    assert d["memory_total_mb"] is None  # None stays None (never 0/sentinel)
    assert d["sources"] == {"memory_used_mb": "probe"}
    json.dumps(d)  # must be JSON-serializable


def test_capabilities_to_dict_shape():
    d = capabilities_to_dict(DeviceCapabilities())
    assert set(d) == {"memory", "utilization", "power", "temperature",
                      "clock", "pressure", "reset"}
    assert all(v is False for v in d.values())


# ── Null backends ────────────────────────────────────────────────

def test_null_telemetry_snapshot_is_all_none():
    s = NullTelemetry().snapshot()
    assert s.platform == "other" and s.accelerator == "unknown"
    assert s.memory_kind == "unknown"
    for f in ("memory_used_mb", "memory_total_mb", "memory_budget_mb",
              "utilization_pct", "power_w", "temperature_c", "clock_mhz",
              "pressure_level"):
        assert getattr(s, f) is None, f
    assert s.sources == {}


def test_null_telemetry_capabilities_all_false():
    c = NullTelemetry().capabilities()
    assert not any((c.memory, c.utilization, c.power, c.temperature,
                    c.clock, c.pressure, c.reset))


def test_null_control_reset_unsupported():
    r = NullDeviceControl().reset()
    assert not r.ok and r.status == "unsupported"


# ── factories ────────────────────────────────────────────────────

def test_factory_unknown_profile_returns_null():
    t = telemetry_for_profile(PlatformProfile("other", "vram", "none"))
    assert isinstance(t, NullTelemetry)
    ctl = device_control_for_profile(PlatformProfile("other", "vram", "none"))
    assert isinstance(ctl, NullDeviceControl)


def test_contract_import_does_not_probe():
    """Importing the contract must not invoke any subprocess (lazy assembly).

    Concrete forced-profile selection is exercised in Task 2, once real
    backends exist; here we prove import-time neutrality: in a fresh
    interpreter, subprocess.run/Popen are poisoned before import — any
    import-time platform command would explode.
    """
    code = (
        "import subprocess\n"
        "def _boom(*a, **k):\n"
        "    raise AssertionError('subprocess at import time')\n"
        "subprocess.run=_boom; subprocess.Popen=_boom\n"
        "import inferfabric.platform, inferfabric.platform.telemetry\n"
        "from inferfabric.platform.telemetry import telemetry_for_profile\n"
        "from inferfabric.platform import PlatformProfile\n"
        "t=telemetry_for_profile(PlatformProfile('other','vram','none'))\n"
        "print('OK' if t.snapshot().memory_used_mb is None else 'BAD')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"
