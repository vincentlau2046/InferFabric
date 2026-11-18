"""DeviceTelemetry T5 — API contract: one reading, JSON null, no GPU subprocess.

Covers: /api/device_metrics + /api/snapshot.device share one cached snapshot
(one probe, one timestamp), /system projects the same reading without running
nvidia-smi, ETag treats the sampling timestamp as volatile only, and the
power routes never contaminate each other's caches.
"""

import json
import time
from types import SimpleNamespace

import pytest

from inferfabric.platform.telemetry import (
    DeviceCapabilities,
    DeviceSnapshot,
    legacy_gpu_fields,
)
from inferfabric.telemetry import TelemetryHub


# ── fakes ────────────────────────────────────────────────────────

class CountingDevice:
    """Static-capability backend; every snapshot() call is counted."""

    def __init__(self, *, timestamp=1234.5, power_w=None,
                 accelerator="apple_silicon", memory_kind="unified"):
        self.calls = 0
        self._kw = dict(timestamp=timestamp, power_w=power_w,
                        accelerator=accelerator, memory_kind=memory_kind)

    def snapshot(self) -> DeviceSnapshot:
        self.calls += 1
        return DeviceSnapshot(
            platform="darwin", accelerator=self._kw["accelerator"],
            memory_kind=self._kw["memory_kind"],
            memory_used_mb=8000, memory_total_mb=None, memory_budget_mb=24576,
            utilization_pct=None, power_w=self._kw["power_w"],
            temperature_c=None, clock_mhz=None, pressure_level=1,
            timestamp=self._kw["timestamp"], sources={"sysctl": ["hw.memsize"]},
        )

    def capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(memory=True, pressure=True)


def _apple_snap(**kw) -> DeviceSnapshot:
    return CountingDevice(**kw).snapshot()


def _snapshot_payload() -> dict:
    return snapshot_payload_of(_apple_snap())


def snapshot_payload_of(snap: DeviceSnapshot) -> dict:
    from inferfabric.platform.telemetry import snapshot_to_dict
    return snapshot_to_dict(snap)


# ── hub observation cache (15s TTL, monotonic) ───────────────────

def test_hub_device_snapshot_ttl_fake_clock(tmp_path):
    hub = TelemetryHub(data_dir=tmp_path, device_telemetry=CountingDevice())
    dev = hub.device_telemetry
    t = {"now": 1000.0}
    hub._clock = lambda: t["now"]  # fake monotonic clock

    s1 = hub.get_device_snapshot()
    assert dev.calls == 1                       # cold start refreshes once
    t["now"] = 1010.0
    assert hub.get_device_snapshot() is s1      # within TTL → same object
    assert dev.calls == 1
    t["now"] = 1016.0                           # > 15s past the refresh
    s2 = hub.get_device_snapshot()
    assert dev.calls == 2                       # one refresh per expiry
    assert s2 is not s1


# ── /api/snapshot composition ────────────────────────────────────

def _run_snapshot_handler(monkeypatch, dev, *, system_info=None):
    """Drive ProxyHandler._handle_snapshot with fake pm; return payload."""
    from inferfabric.proxy import handler as hmod

    exp_status = {"gpu_mode": "idle", "gpu_used_mb": None,
                  "gpu_total_mb": None, "gpu_util_pct": None,
                  "active_services": []}
    monkeypatch.setattr(hmod, "_compute_expensive",
                        lambda pm: {"status": dict(exp_status),
                                    "metrics_24h": {}})

    class HubCache:
        """Same TTL contract as TelemetryHub.get_device_snapshot."""

        def __init__(self):
            self._snap = None
            self._at = None

        def get_device_snapshot(self):
            now = time.monotonic()
            if self._snap is None or now - self._at >= 15.0:
                self._snap = dev.snapshot()
                self._at = now
            return self._snap

    tel = SimpleNamespace(
        device_telemetry=dev, get_device_snapshot=HubCache().get_device_snapshot,
        query_request_log=lambda **k: [],
        token_collector=SimpleNamespace(_load_full_state=lambda: {}),
    )
    mgr = SimpleNamespace(
        list_models=lambda: [], _models={},
        state=SimpleNamespace(get_history=lambda n: []),
    )
    pm = SimpleNamespace(mgr=mgr, telemetry=tel)

    captured = {}

    class _Shell:
        """Handler shell: real _system_info binding, stubbed HTTP writer."""
        _system_info = hmod.ProxyHandler._system_info
        if system_info is not None:  # per-test override (still bound)
            _system_info = system_info

        def __init__(self):
            self.headers = {}

        def send_response(self, code):
            captured["code"] = code

        def send_header(self, k, v):
            captured.setdefault("headers", {})[k] = v

        def end_headers(self):
            pass

        def _safe_write(self, body):
            captured["body"] = body

    hmod.ProxyHandler._handle_snapshot(_Shell(), pm)
    return json.loads(captured["body"])


def test_snapshot_handler_uses_real_system_info(monkeypatch):
    """Regression: /api/snapshot must not double-convert capabilities.

    _handle_snapshot passes raw caps into _system_info; passing the already
    converted dict crashed the live endpoint ('dict' has no .memory).
    """
    from inferfabric.proxy import handler as hmod
    dev = CountingDevice(timestamp=55.0)
    payload = _run_snapshot_handler(
        monkeypatch, dev, system_info=hmod.ProxyHandler._system_info)
    assert payload["system"]["device"]["capabilities"]["memory"] is True
    assert payload["system"]["device"]["snapshot"]["timestamp"] == 55.0


def test_device_routes_share_one_probe_and_timestamp(monkeypatch):
    dev = CountingDevice(timestamp=1234.5)
    payload = _run_snapshot_handler(monkeypatch, dev)
    assert dev.calls == 1  # one provider probe feeds every consumer
    env = payload["device"]
    assert env["snapshot"]["timestamp"] == 1234.5
    assert env["capabilities"]["memory"] is True
    # /system projects the very same reading
    assert payload["system"]["device"]["snapshot"]["timestamp"] == 1234.5
    # legacy status.gpu_* derived from the same reading (Apple mapping §14.2)
    assert payload["status"]["gpu_used_mb"] == 8000
    assert payload["status"]["gpu_total_mb"] == 24576   # unified → budget
    assert payload["status"]["gpu_util_pct"] is None    # never fabricated


def test_snapshot_etag_changes_when_device_value_changes():
    from inferfabric.proxy.handler import _snapshot_etag

    s1 = snapshot_payload_of(_apple_snap(power_w=None, timestamp=100.0))
    s2 = snapshot_payload_of(_apple_snap(power_w=55.0, timestamp=100.0))
    s3 = snapshot_payload_of(_apple_snap(power_w=None, timestamp=999.0))

    def etag_for(dev_snap):
        payload = {"device": {"snapshot": dev_snap, "capabilities": {}},
                   "system": {"device": {"snapshot": dev_snap}}}
        return _snapshot_etag(payload)

    assert etag_for(s1) != etag_for(s2)  # value change → new etag
    assert etag_for(s1) == etag_for(s3)  # timestamp alone is volatile-only


# ── /system: no GPU subprocess, snapshot projection ──────────────

def test_system_info_uses_snapshot_no_subprocess(monkeypatch):
    import subprocess
    from inferfabric.proxy.handler import ProxyHandler

    def _boom(*a, **kw):
        raise AssertionError("nvidia-smi subprocess must not run")

    monkeypatch.setattr(subprocess, "run", _boom)
    snap = _apple_snap()
    info = ProxyHandler._system_info(SimpleNamespace(), snap)
    # unavailable device values stay None (never 0)
    assert info["gpu_util_pct"] is None
    assert info["gpu_power_w"] is None
    assert info["device"]["snapshot"]["timestamp"] == snap.timestamp


# ── /api/device_metrics envelope ─────────────────────────────────

def test_device_metrics_endpoint_envelope(monkeypatch):
    from inferfabric.proxy import handler as hmod

    dev = CountingDevice(timestamp=42.0)
    tel = SimpleNamespace(device_telemetry=dev,
                          get_device_snapshot=lambda: dev.snapshot())
    pm = SimpleNamespace(telemetry=tel)
    out = {}
    fake = SimpleNamespace(_send_json=lambda data, code=200: out.update(
        data=data, code=code))

    hmod.ProxyHandler._handle_api_device_metrics(fake, pm)
    assert out["code"] == 200
    assert set(out["data"].keys()) == {"snapshot", "capabilities"}
    snap = out["data"]["snapshot"]
    assert snap["timestamp"] == 42.0
    assert snap["sources"] == {"sysctl": ["hw.memsize"]}
    assert snap["power_w"] is None and snap["memory_total_mb"] is None
    assert out["data"]["capabilities"]["memory"] is True


def test_device_metrics_unavailable_stays_null(monkeypatch):
    from inferfabric.proxy import handler as hmod

    class DeadBackend:
        def snapshot(self):
            return DeviceSnapshot(
                platform="other", accelerator="unknown", memory_kind="unknown",
                memory_used_mb=None, memory_total_mb=None, memory_budget_mb=None,
                utilization_pct=None, power_w=None, temperature_c=None,
                clock_mhz=None, pressure_level=None, timestamp=7.0, sources={})

        def capabilities(self):
            return DeviceCapabilities()

    pm = SimpleNamespace(telemetry=SimpleNamespace(
        device_telemetry=DeadBackend(),
        get_device_snapshot=lambda: DeadBackend().snapshot()))
    out = {}
    fake = SimpleNamespace(_send_json=lambda data, code=200: out.update(data=data))
    hmod.ProxyHandler._handle_api_device_metrics(fake, pm)
    snap = out["data"]["snapshot"]
    assert all(snap[k] is None for k in
               ("memory_used_mb", "memory_total_mb", "power_w",
                "utilization_pct", "temperature_c", "clock_mhz"))


# ── power routes: separate caches, separate streams ──────────────

def test_power_routes_do_not_contaminate_caches(monkeypatch):
    from inferfabric.proxy import handler as hmod

    monkeypatch.setattr(hmod, "_power_series_cache",
                        {g: hmod._ExpensiveCache(ttl=300.0)
                         for g in hmod._POWER_CACHE_TTL_GRANS})
    monkeypatch.setattr(hmod, "_device_power_series_cache",
                        {g: hmod._ExpensiveCache(ttl=300.0)
                         for g in hmod._POWER_CACHE_TTL_GRANS})

    tel = SimpleNamespace(
        get_power_series=lambda gran: {"stream": "legacy", "gran": gran},
        get_device_power_series=lambda gran: {"stream": "aggregate", "gran": gran},
    )
    pm = SimpleNamespace(telemetry=tel)
    legacy_out, device_out = {}, {}
    fl = SimpleNamespace(path="/api/power?gran=hour",
                         _send_json=lambda d, c=200: legacy_out.update(data=d))
    fd = SimpleNamespace(path="/api/device_power?gran=hour",
                         _send_json=lambda d, c=200: device_out.update(data=d))

    hmod.ProxyHandler._handle_api_power(fl, pm)
    hmod.ProxyHandler._handle_api_device_power(fd, pm)

    assert legacy_out["data"]["stream"] == "legacy"
    assert device_out["data"]["stream"] == "aggregate"


# ── legacy field mapping helper ──────────────────────────────────

def test_legacy_gpu_fields_mapping():
    apple = _apple_snap()  # unified: used 8000, budget 24576
    assert legacy_gpu_fields(apple) == {
        "gpu_used_mb": 8000, "gpu_total_mb": 24576, "gpu_util_pct": None}

    nvidia = DeviceSnapshot(
        platform="linux", accelerator="nvidia", memory_kind="vram",
        memory_used_mb=300, memory_total_mb=3000, memory_budget_mb=2910,
        utilization_pct=10.0, power_w=40.0, temperature_c=50.0,
        clock_mhz=1500, pressure_level=None, timestamp=1.0, sources={})
    assert legacy_gpu_fields(nvidia) == {
        "gpu_used_mb": 300, "gpu_total_mb": 3000,
        "gpu_util_pct": 300 / 3000 * 100}

    unknown = DeviceSnapshot(
        platform="other", accelerator="unknown", memory_kind="unknown",
        memory_used_mb=None, memory_total_mb=None, memory_budget_mb=None,
        utilization_pct=None, power_w=None, temperature_c=None,
        clock_mhz=None, pressure_level=None, timestamp=1.0, sources={})
    assert legacy_gpu_fields(unknown) == {
        "gpu_used_mb": None, "gpu_total_mb": None, "gpu_util_pct": None}


def test_hub_device_snapshot_single_flight(tmp_path):
    """Concurrent observers at expiry must produce ONE provider probe."""
    import threading
    from inferfabric.platform.telemetry import DeviceCapabilities

    class SlowDevice(CountingDevice):
        def snapshot(self):
            time.sleep(0.25)
            return super().snapshot()

    hub = TelemetryHub(data_dir=tmp_path, device_telemetry=SlowDevice())
    hub._clock = time.monotonic
    out = []
    def probe():
        out.append(hub.get_device_snapshot())
    threads = [threading.Thread(target=probe) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert hub.device_telemetry.calls == 1          # single flight
    assert all(s is out[0] for s in out)            # one shared reading
