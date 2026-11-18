"""
inferfabric/health.py — HTTP health checking + GPU memory helpers.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).
"""

import os
import time
import signal
import logging
import subprocess
from pathlib import Path
from typing import Optional

from .config import (
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    STOP_SIGTERM_TIMEOUT,
)

log = logging.getLogger("inferfabric")


# ─── Shell Helpers ───────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# ─── GPU Memory ──────────────────────────────────────────────────

def _default_device_telemetry():
    """Assemble the platform telemetry backend (single factory entry)."""
    from .platform import detect
    from .platform.telemetry import telemetry_for_profile
    return telemetry_for_profile(detect())


def _device_snapshot(telemetry=None):
    tel = telemetry if telemetry is not None else _default_device_telemetry()
    return tel.snapshot()


def gpu_used_mb(telemetry=None) -> int | None:
    """DEPRECATED compat shim — device telemetry is the source of truth.

    Returns snapshot.memory_used_mb; None when unavailable (fail-open for
    observations). Callers on admission/control paths must handle None
    (fail-closed), never treat it as 0.
    """
    return _device_snapshot(telemetry).memory_used_mb


def gpu_total_mb(telemetry=None) -> int | None:
    """DEPRECATED compat shim — legacy "total" mapping per memory_kind.

    unified (Apple): budget B = RAM − reserve (spec §14.2 — wait_gpu_free
    and legacy UI keep budget semantics). vram (NVIDIA): physical VRAM
    total. None when unavailable.
    """
    s = _device_snapshot(telemetry)
    if s.memory_kind == "unified":
        return s.memory_budget_mb
    return s.memory_total_mb


def wait_gpu_free(timeout: int = GPU_FREE_TIMEOUT,
                  threshold_mb: int = GPU_FREE_THRESHOLD_MB,
                  telemetry=None) -> bool:
    """Wait for device memory to drop below threshold. False on timeout.

    Fail-closed: an unavailable reading is never treated as free; polling
    continues until the deadline, then returns False.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        used = gpu_used_mb(telemetry)
        if used is not None and used < threshold_mb:
            return True
        time.sleep(2)
    return False


# ─── HTTP Health ─────────────────────────────────────────────────

def check_http_status(url: str, timeout: int = 3, retries: int = 3) -> str:
    """Check HTTP endpoint: '✅' (200), '⏳' (503 loading), '❌' (unreachable/error).
    Retries on transient failure to avoid false negatives from GC pauses / load spikes."""
    import urllib.request
    import urllib.error
    for attempt in range(retries):
        try:
            resp = urllib.request.urlopen(url, timeout=timeout)
            try:
                if resp.status == 200:
                    return "✅"
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            if e.code == 503:
                return "⏳"
            log.debug("HTTP %d from %s (attempt %d/%d)", e.code, url, attempt + 1, retries)
        except Exception:
            log.debug("HTTP check failed from %s (attempt %d/%d)", url, attempt + 1, retries)

    log.debug("HTTP check failed after %d retries: %s", retries, url)
    return "❌"


def wait_http(url: str, timeout: int = 300) -> bool:
    """Wait for HTTP endpoint to return 200. Respects 503 as transient (loading).
    Returns True if healthy within timeout, False otherwise."""
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    consecutive_non_503_errors = 0
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            try:
                if resp.status == 200:
                    return True
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            if e.code == 503:
                consecutive_non_503_errors = 0
            else:
                consecutive_non_503_errors += 1
                if consecutive_non_503_errors >= 3:
                    log.error("HTTP %d from %s 3 times consecutively — giving up", e.code, url)
                    return False
        except Exception:
            consecutive_non_503_errors = 0
        time.sleep(3)
    return False


# ─── PID File Kill ───────────────────────────────────────────────

def kill_port(pidfile: Path, timeout: int = STOP_SIGTERM_TIMEOUT) -> None:
    """Kill a process by PID file, with SIGKILL fallback."""
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, PermissionError):
        pidfile.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(timeout):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pidfile.unlink(missing_ok=True)
                return
            time.sleep(1)
        log.warning("SIGTERM failed for PID %d, sending SIGKILL", pid)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        pidfile.unlink(missing_ok=True)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
