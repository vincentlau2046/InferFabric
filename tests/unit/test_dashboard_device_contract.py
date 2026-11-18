"""DeviceTelemetry T5 — Dashboard source-level contract (null semantics).

The dashboard is served as native JS (no SPA build), so these are static
source contract tests in the project's existing style: the device paths in
store/overview/monitor must preserve JSON null, key off memory_kind, and
carry no hard-coded device limits (5090D TDP 575/600W, 3110MHz, 32GB, 32607).
"""

from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[2] / "inferfabric" / "dashboard" / "js"


def _src(name: str) -> str:
    return (JS_DIR / name).read_text(encoding="utf-8")


# ── store.js: null passthrough + device state ────────────────────

def test_store_has_no_sentinel_fallbacks():
    src = _src("store.js")
    assert "32607" not in src                       # fake 32GB VRAM sentinel
    assert "gpu_used_mb || 0" not in src            # null must survive
    assert "gpu_total_mb ||" not in src
    assert "gpu_util_pct || 0" not in src
    assert "gpu_power_w ||" not in src


def test_store_exposes_device_state_and_memory_kind():
    src = _src("store.js")
    assert "memory_kind" in src
    assert "snap.device" in src or "device" in src  # device envelope retained
    assert "capabilities" in src                    # capability state exposed
    # pct derivation must not fabricate a percentage from null readings
    assert "gpuUsed != null && gpuTotal" in src


# ── overview.js: unified labels, no 5090D limits ─────────────────

def test_overview_handles_unified_and_null():
    src = _src("overview.js")
    assert "unified" in src                         # memory_kind-aware label
    assert "memory_kind" in src
    # utilization/power render '—' when null, never 0
    assert "util.pct == null" in src
    assert "util.power == null" in src


def test_overview_has_no_hardcoded_device_limits():
    src = _src("overview.js")
    assert "575" not in src                         # 5090D TDP
    assert "3110" not in src                        # 5090D max clock
    assert "5090" not in src


# ── monitor.js: no fixed 600W axis ───────────────────────────────

def test_monitor_power_axis_is_data_driven():
    src = _src("monitor.js")
    assert "max: 600" not in src                    # fixed TDP-capped axis
    assert "5090" not in src
    # W axis derives its max from actual samples (or no-scale empty state)
    assert "_niceCeil" in src


def test_monitor_renders_unavailable_power_as_dash():
    src = _src("monitor.js")
    # avg power summary keeps the null → '—' contract
    assert "avg_w != null" in src
