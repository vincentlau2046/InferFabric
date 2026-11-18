"""Platform profile detection — platform/ is the only OS-aware layer (F2)."""
import importlib
import subprocess
import sys

import pytest


def _detect_with_env(monkeypatch, value):
    monkeypatch.setenv("IFF_FORCE_PLATFORM", value)
    import inferfabric.platform as pf
    importlib.reload(pf)
    return pf.detect()


def test_detect_darwin_uses_umem_launchd(monkeypatch):
    p = _detect_with_env(monkeypatch, "darwin")
    assert (p.os, p.resource_backend, p.procguard) == ("darwin", "umem", "launchd")


def test_detect_linux_uses_vram_backend(monkeypatch):
    """Upstream pick safety: a CUDA box must resolve to the vram backend
    without touching any darwin-only probe (Review Focus 1)."""
    p = _detect_with_env(monkeypatch, "linux")
    assert (p.os, p.resource_backend, p.procguard) == ("linux", "vram", "systemd")


def test_detect_unknown_platform_falls_back(monkeypatch):
    p = _detect_with_env(monkeypatch, "freebsd")
    assert (p.os, p.resource_backend, p.procguard) == ("other", "vram", "none")


def test_detect_follows_sys_platform(monkeypatch):
    monkeypatch.delenv("IFF_FORCE_PLATFORM", raising=False)
    import inferfabric.platform as pf
    importlib.reload(pf)
    expected = sys.platform
    p = pf.detect()
    if expected == "darwin":
        assert p.os == "darwin"
    else:
        assert p.os in ("linux", "other")


def test_platform_import_isolation():
    """platform/ must not drag the gateway core into sys.modules (no cycles)."""
    code = (
        "import sys; import inferfabric.platform as pf; pf.detect();"
        "bad=[m for m in ('inferfabric.manager','inferfabric.model_pool',"
        "'inferfabric.proxy_manager') if m in sys.modules];"
        "print('PULLED:' + ','.join(bad) if bad else 'ISOLATED')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert "ISOLATED" in out.stdout, out.stdout + out.stderr
