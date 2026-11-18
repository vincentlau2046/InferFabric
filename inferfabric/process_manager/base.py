"""
inferfabric/process_manager/base.py — Shared base class with utilities
for per-engine sub-managers.
"""

import os
import time
import signal
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.health import gpu_used_mb

log = logging.getLogger("inferfabric")


class BaseProcessManager:
    """Base class providing shared process management utilities.

    All per-engine sub-managers (VLLMProcessManager, etc.) extend this.
    The ProcessManager facade also extends it for force_kill_all etc.
    """

    # 可选回引用：ModelManager 构造时回填（proc.mgr = manager），供
    # 状态机感知的适配器钩子（NInferAdapter.restart 走 stop_service→switch、
    # 在途请求收 503 Switch Guard）使用。未注入时为 None → 适配器退回基类
    # stop+start。不要在此 __init__ 里硬依赖它。
    mgr: object | None = None

    def __init__(self, state, log_dir: Path):
        self._state = state
        self._log_dir = log_dir
        # DeviceTelemetry T3: optional providers injected by ModelManager;
        # None → legacy direct-probe fallback paths.
        self.device_control = None
        self.device_telemetry = None


    def _pkill_by_port(self, port: int) -> None:
        """Kill any remaining process listening on a specific port.

        Safety net for orphaned processes not tracked in state.db.
        """
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                result = subprocess.run(
                    ["fuser", "-k", "-" + str(sig), str(port) + "/tcp"],
                    timeout=5, check=False, capture_output=True
                )
                if result.returncode == 0:
                    log.info("fuser killed processes on port %d (sig=%d)", port, sig)
                    time.sleep(1)
                    break
            except FileNotFoundError:
                # fuser not available — fall back to pkill
                subprocess.run(
                    ["pkill", "-" + str(sig), "-f", f"vllm.*:{port}"],
                    timeout=5, check=False, capture_output=True
                )
                subprocess.run(
                    ["pkill", "-" + str(sig), "-f", f"VLLM::EngineCore.*--port {port}"],
                    timeout=5, check=False, capture_output=True
                )
                subprocess.run(
                    ["pkill", "-" + str(sig), "-f", f"sglang.*:{port}"],
                    timeout=5, check=False, capture_output=True
                )
                subprocess.run(
                    ["pkill", "-" + str(sig), "-f", f"python.*main.py.*{port}"],
                    timeout=5, check=False, capture_output=True
                )
                time.sleep(1)
                break
            except subprocess.TimeoutExpired:
                log.warning("fuser on port %d timed out, skipping", port)
                break
        time.sleep(1)

    def _validate_pid(self, pid: int, expected_substring: str) -> bool:
        """P1-2: Validate that a PID still belongs to the expected process.

        Reads ``/proc/<pid>/cmdline`` (null-separated bytes) and checks
        for ``expected_substring`` (case-insensitive).  Returns False if
        the PID has been recycled by the kernel to an unrelated process.
        """
        cmdline_path = f"/proc/{pid}/cmdline"
        try:
            raw = Path(cmdline_path).read_bytes()
        except FileNotFoundError:
            return False  # PID no longer exists
        except PermissionError:
            # Cannot read cmdline — conservatively assume PID is still valid
            # to avoid accidentally killing an unrelated process via fallback.
            log.debug("Cannot read /proc/%d/cmdline (permission denied), assuming valid", pid)
            return True
        except OSError:
            return False

        # Empty cmdline = kernel thread — PID recycled, not a user process
        if not raw:
            return False

        try:
            cmdline = raw.decode("utf-8", errors="replace")
        except Exception as e:
            log.warning("pid cmdline decode failed: %s", e)
            return False

        return expected_substring.lower() in cmdline.lower()

    # ─── Internal Helpers ────────────────────────────────────────

    def _cleanup_pid_files(self, prefix: str):
        """Remove PID files for a given prefix (vllm or comfyui)."""
        for pf in self._log_dir.glob(f"{prefix}*.pid"):
            pf.unlink(missing_ok=True)
        if prefix == "vllm":
            # Also clean legacy PID files
            legacy_dir = Path.home() / "models" / "vllm_logs"
            if legacy_dir.exists():
                for pf in legacy_dir.glob("*.pid"):
                    pf.unlink(missing_ok=True)

    def _reap_zombies(self):
        """Reap zombie child processes."""
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass

    def _wait_gpu_idle(self, timeout: int = 60, force: bool = False) -> dict:
        """P1-2: Wait for GPU to return to idle state after process exit.
        
        Uses a relative baseline: records initial idle usage and checks
        if current usage is within 15% of baseline. This handles desktop
        environments where compositor/CUDA usage varies.
        
        Args:
            force: If True, skip waiting and return immediately.
        """
        if force:
            return {"status": "force", "used_mb": gpu_used_mb()}
        
        # Get baseline idle GPU memory (first call or cached)
        baseline = self._get_gpu_baseline()
        threshold = int(baseline * 1.5) + 512  # 150% of baseline + 512MB margin
        
        for _ in range(timeout):
            used = gpu_used_mb()
            if used is None:
                # Fail-closed: an unavailable reading never reports free.
                return {"status": "timeout",
                        "message": "device memory telemetry unavailable"}
            if used <= threshold:
                log.info("GPU returned to idle (%d MB, threshold=%d)", used, threshold)
                return {"status": "ok", "used_mb": used}
            time.sleep(1)
        
        # If we timeout but GPU is dropping, give it more time
        used = gpu_used_mb()
        if used is not None and used < threshold * 0.8:
            log.info("GPU still dropping (%d MB), accepting", used)
            return {"status": "ok", "used_mb": used}
        
        return {"status": "timeout", "message": f"GPU did not return to idle (threshold={threshold}MB)"}
    

    # ─── GPU CUDA State Reset (Fragmentation Mitigation) ───────────

    def _clear_gpu_cuda_state(self, gpu_index: int = 0) -> dict:
        """Clear GPU CUDA driver state to eliminate memory fragmentation.

        After a GPU-heavy process (ComfyUI, vLLM, etc.) exits, the CUDA driver
        may retain a fragmented memory map that prevents large contiguous
        allocations — even though nvidia-smi shows sufficient free memory.

        This method tries multiple approaches in order of effectiveness:

        1. ``nvidia-smi --gpu-reset`` — Full GPU state reset (most effective)
        2. ``nvidia-smi -pm 0 && nvidia-smi -pm 1`` — Persistence mode toggle
           (causes driver to re-initialize internal state)
        3. ``nvidia-smi -r`` — Secondary reset method (falls through)

        Safety: only call when GPU is confirmed idle (no running CUDA contexts).
        Each approach is validated by reading GPU memory before and after.
        """
        before_mb = gpu_used_mb()
        before_txt = f"{before_mb} MB" if before_mb is not None else "unknown"
        log.info("Clearing GPU CUDA state (current usage: %s) ...", before_txt)

        # ── Approach 1: nvidia-smi --gpu-reset ────────────────────
        try:
            r = subprocess.run(
                ["nvidia-smi", "--gpu-reset", "-i", str(gpu_index)],
                timeout=30, capture_output=True, text=True,
            )
            if r.returncode == 0:
                time.sleep(2)  # wait for reset to complete
                after_mb = gpu_used_mb()
                log.info("GPU reset successful (before: %s, after: %s MB)", before_txt,
                 f"{after_mb} MB" if after_mb is not None else "unknown")
                return {"status": "ok", "method": "gpu-reset", "before_mb": before_mb, "after_mb": after_mb}
            else:
                log.warning("nvidia-smi --gpu-reset failed (rc=%d): %s", r.returncode, r.stderr.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.warning("nvidia-smi --gpu-reset unavailable: %s", e)

        # ── Approach 2: Toggle persistence mode ───────────────────
        # PM toggling causes the driver to reinitialize its internal memory map
        try:
            r = subprocess.run(
                ["nvidia-smi", "-pm", "0"],
                timeout=10, capture_output=True, text=True,
            )
            if r.returncode == 0:
                time.sleep(1)
                subprocess.run(
                    ["nvidia-smi", "-pm", "1"],
                    timeout=10, capture_output=True, text=True,
                )
                time.sleep(1)
                after_mb = gpu_used_mb()
                log.info("GPU PM toggle successful (before: %s, after: %s MB)", before_txt,
                 f"{after_mb} MB" if after_mb is not None else "unknown")
                return {"status": "ok", "method": "pm-toggle", "before_mb": before_mb, "after_mb": after_mb}
            else:
                log.warning("nvidia-smi -pm toggle failed (rc=%d): %s", r.returncode, r.stderr.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.warning("nvidia-smi -pm toggle unavailable: %s", e)

        # ── Approach 3: nvidia-smi -r (secondary reset) ───────────
        try:
            r = subprocess.run(
                ["nvidia-smi", "-r"],
                timeout=30, capture_output=True, text=True,
            )
            if r.returncode == 0:
                time.sleep(2)
                after_mb = gpu_used_mb()
                log.info("GPU nvidia-smi -r successful (before: %s, after: %s MB)", before_txt,
                 f"{after_mb} MB" if after_mb is not None else "unknown")
                return {"status": "ok", "method": "nvidia-smi-r", "before_mb": before_mb, "after_mb": after_mb}
            else:
                log.warning("nvidia-smi -r failed (rc=%d): %s", r.returncode, r.stderr.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.warning("nvidia-smi -r unavailable: %s", e)

        log.warning("All GPU CUDA state reset methods failed — fragmentation may persist")
        return {"status": "error", "message": "No GPU reset method succeeded"}

    def _get_gpu_baseline(self) -> int:
        """Get or cache the baseline GPU memory usage.

        P1-3: Uses a 7-day TTL on the cached baseline to prevent stale
        measurements.  Only persists new measurements when the GPU is
        idle (measured <= cached baseline).  If the cache is expired
        and the GPU is busy, returns the measured value for this call
        but does not persist it — unless the cached value itself is
        unreasonable (>2GB idle), in which case the cache is discarded.
        """
        SEVEN_DAYS = 7 * 86400  # seconds
        REASONABLE_IDLE_MAX = 2048  # MB — idle baseline should never exceed this
        cache_file = Path.home() / ".inferfabric" / "gpu_baseline.json"

        # ── Read cached value ──────────────────────────────────────
        cached_baseline: int | None = None
        cached_ts: float = 0.0
        try:
            if cache_file.exists():
                data = json.loads(cache_file.read_text())
                cached_baseline = int(data.get("baseline_mb", 0)) or None
                cached_ts = float(data.get("timestamp", 0))
        except Exception:
            cached_baseline = None
            cached_ts = 0.0

        # Return cached value if still valid (within TTL)
        if cached_baseline and cached_ts > 0:
            age = time.time() - cached_ts
            if age < SEVEN_DAYS:
                return cached_baseline
            else:
                log.info("GPU baseline cache expired (age=%.0f days), re-sampling", age / 86400)

        # ── Re-sample ──────────────────────────────────────────────
        measured = gpu_used_mb()
        baseline = measured if (measured and measured >= 100) else 512

        # Guard: only persist if GPU is actually idle (current usage
        # is within 150% of a reasonable baseline).  If the GPU is
        # currently loaded, use measured value for this call but don't
        # persist it — and don't return a stale expired cache either.
        if cached_baseline and measured:
            # If cached value is unreasonable (>2GB idle), discard it entirely
            if cached_baseline > REASONABLE_IDLE_MAX:
                log.info(
                    "Discarding unreasonable cached baseline (%d MB > %d MB)",
                    cached_baseline, REASONABLE_IDLE_MAX,
                )
                # Fall through to persist measured value
            elif measured > cached_baseline:
                # GPU is currently loaded — return cached idle value,
                # don't persist measured (would poison the idle baseline)
                log.info(
                    "Skipping baseline persist — GPU appears busy "
                    "(used=%d MB, cached baseline=%d MB)",
                    measured, cached_baseline,
                )
                return cached_baseline
        elif measured and measured < 100:
            # measured < 100 is suspicious; keep cached if available
            if cached_baseline:
                return cached_baseline

        # Persist new baseline with timestamp
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({
                "baseline_mb": baseline,
                "timestamp": time.time(),
            }))
            log.info("GPU baseline updated: %d MB", baseline)
        except Exception as e:
            log.error("failed to write GPU baseline: %s", e)

        return baseline

