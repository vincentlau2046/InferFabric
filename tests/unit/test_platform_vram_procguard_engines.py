"""Upstreamable platform units: VRAM ledger backend, procguard, engine matrix.

VramThreeStateLedger must work on any box (Review Focus 1) — the gpu module
is injected, never imported at module scope; unit tests use fakes.
"""
import subprocess
import sys

from inferfabric.platform.resource import Lifecycle, Mode


# ── VRAM backend ───────────────────────────────────────────────

class _FakeGpu:
    def __init__(self, blocks=False):
        self.blocks = blocks
        self.asked = None

    def would_block(self, name, needed_mb, exclusive):
        self.asked = (name, needed_mb, exclusive)
        return self.blocks


def _vram(budget=8000, gpu=None):
    from inferfabric.platform.resource_vram import VramThreeStateLedger
    return VramThreeStateLedger(budget_mb=budget, gpu=gpu)


def test_vram_admit_within_budget():
    led = _vram(budget=8000)
    led.register("m", 4000, Mode.ON_DEMAND)
    d = led.admit("m")
    assert d.ok and d.needed_mb == 4000


def test_vram_admit_rejects_over_budget_english_reason():
    led = _vram(budget=8000)
    led.register("big", 9000, Mode.ON_DEMAND)
    d = led.admit("big")
    assert not d.ok and "VRAM" in d.reason


def test_vram_gpu_gate_blocks_and_force_overrides():
    gpu = _FakeGpu(blocks=True)
    led = _vram(budget=8000, gpu=gpu)
    led.register("b", 1000, Mode.ON_DEMAND)
    d = led.admit("b")
    assert not d.ok and gpu.asked == ("b", 1000, True)
    assert led.admit("b", force=True).ok


def test_vram_reuses_lifecycle_and_sleep_semantics():
    led = _vram(budget=8000)
    e = led.register("od", 1000, Mode.ON_DEMAND, idle_timeout_min=5)
    led.mark_ready("od")
    e.last_used_ts -= 6 * 60
    assert led.idle_sleep_candidates() == ["od"]
    led.mark_sleeping("od")
    assert led.registered("od").state is Lifecycle.SLEEPING


def test_vram_resident_total_counts_ready_not_stopped():
    led = _vram(budget=8000)
    a = led.register("a", 1000, Mode.RESIDENT)
    led.register("b", 1000, Mode.RESIDENT)
    led.mark_ready("a")
    assert led.resident_total_mb() == 1000  # stopped entries don't count


# ── ProcGuard ──────────────────────────────────────────────────

def test_launchd_plist_render_contains_essentials():
    from inferfabric.platform.procguard import render_launchd_plist
    plist = render_launchd_plist(
        label="com.inferfabric.gateway",
        program_args=["/bin/sh", "-lc", "exec gateway"],
        keep_alive=True,
    )
    assert "com.inferfabric.gateway" in plist
    assert "KeepAlive" in plist and "true" in plist
    assert "/bin/sh" in plist


def test_null_guard_is_foreground_noop():
    from inferfabric.platform.procguard import NullProcGuard
    g = NullProcGuard()
    assert g.install() is True and g.uninstall() is True
    assert g.status() == "foreground"


def test_launchd_install_fails_when_launchctl_rejects(monkeypatch, tmp_path):
    """Fail-visible contract: install() must return False if launchctl
    rejects the plist (Final review Important 1)."""
    import inferfabric.platform.procguard as pg

    def fake_run(*a, **k):
        m = type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        return m

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    g = pg.LaunchdProcGuard("com.test.guard", ["/bin/true"])
    assert g.install() is False


def test_launchd_install_succeeds_when_launchctl_ok(monkeypatch, tmp_path):
    import inferfabric.platform.procguard as pg

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    monkeypatch.setattr(pg.Path, "home", lambda: tmp_path)
    g = pg.LaunchdProcGuard("com.test.guard", ["/bin/true"])
    assert g.install() is True
    assert (tmp_path / "Library" / "LaunchAgents" / "com.test.guard.plist").exists()


def test_systemd_install_enables_unit_and_fails_on_error(monkeypatch, tmp_path):
    """systemd install must enable --now (not just daemon-reload) and fail
    visibly when systemctl rejects the unit."""
    import inferfabric.platform.procguard as pg

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    monkeypatch.setattr(pg.Path, "home", lambda: tmp_path)
    g = pg.SystemdProcGuard("inferfabric-gateway", ["/bin/true"])
    assert g.install() is True
    joined = " ".join(" ".join(c) for c in calls)
    assert "enable" in joined and "--now" in joined

    monkeypatch.setattr(pg.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())
    assert g.install() is False


def test_guard_factory_requires_explicit_identity():
    """guard_for_profile must not fabricate a production label — the caller
    supplies the real label/args (prevents overwriting the live plist)."""
    import pytest
    from inferfabric.platform import detect
    from inferfabric.platform.procguard import guard_for_profile
    with pytest.raises(TypeError):
        guard_for_profile(detect())
    g = guard_for_profile(detect(), label="com.example.gw",
                          program_args=["/bin/true"])
    assert g is not None


# ── Engine matrix ──────────────────────────────────────────────

def test_engine_matrix_split_by_platform():
    from inferfabric.platform.engines import ENGINES
    assert "llama_cpp" in ENGINES["darwin"] and "vllm" not in ENGINES["darwin"]
    assert "vllm" in ENGINES["linux"] and "ocr_helper" not in ENGINES["linux"]


def test_engines_module_import_isolation():
    code = (
        "import sys, inferfabric.platform.engines;"
        "bad=[m for m in ('inferfabric.manager','inferfabric.process_manager') if m in sys.modules];"
        "print('PULLED' if bad else 'ISOLATED')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "ISOLATED" in out.stdout
