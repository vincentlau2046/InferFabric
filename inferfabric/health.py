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
from .platform import probe  # platform-layer probes (F2)

log = logging.getLogger("inferfabric")


# ─── Shell Helpers ───────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# ─── GPU Memory ──────────────────────────────────────────────────

def gpu_used_mb() -> int:
    """Get total GPU memory used across all GPUs.

    Platform (F2): on unified-memory hosts (macOS) this maps to the system
    memory watermark — there is no discrete VRAM to query.
    """
    if probe.IS_MACOS:
        used = probe.mem_used_mb()
        return used if used is not None else 0
    try:
        r = _run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
        total = sum(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip())
        return total
    except Exception:
        return 0


def gpu_total_mb() -> int:
    """Get total GPU memory.

    Platform (F2): on unified-memory hosts (macOS) this returns the
    inference budget B = RAM − reserve, so wait_gpu_free and other
    upstream logic keep correct semantics there.
    """
    if probe.IS_MACOS:
        return probe.budget_mb()
    try:
        r = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        total = sum(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip())
        return total
    except Exception:
        return 32607  # fallback for RTX 5090D


def wait_gpu_free(timeout: int = GPU_FREE_TIMEOUT, threshold_mb: int = GPU_FREE_THRESHOLD_MB) -> bool:
    """Wait for GPU memory to drop below threshold. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gpu_used_mb() < threshold_mb:
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
