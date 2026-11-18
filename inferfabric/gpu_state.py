"""
inferfabric/gpu_state.py — GPU state machine (extracted from manager.py v4.0).

Responsible for: scanning actual GPU processes, deriving GPU mode, detecting
orphan PIDs, restoring dead PIDs, state reconciliation, VRAM queries, force reset.
"""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import (
    GPU_FREE_THRESHOLD_MB,
    ModelConfig,
)
from .config_watcher import detect_drift as _detect_drift
from .health import (
    check_http_status,
    gpu_used_mb,
    gpu_total_mb,
    wait_gpu_free,
)
from .state import GPUMode, ServiceState, StateDB

log = logging.getLogger("inferfabric")


class GpuStateMachine:
    """GPU state machine — compares DB state against actual GPU processes.

    Owns the reconciliation logic, orphan detection, and force-reset.
    """

    def __init__(self, state, proc, health, lock, models,
                 device_telemetry=None):
        self.state = state
        self._proc = proc
        self._health = health
        self._lock = lock
        self._models = models
        self._device_telemetry = device_telemetry

    # ── Port / Process Helpers ────────────────────────────────────

    @staticmethod
    def _port_pid(port: int) -> Optional[int]:
        """Return PID owning a TCP port, or None.

        Delegated to the platform probe layer: macOS uses lsof (no fuser),
        Linux keeps the upstream fuser behavior.
        """
        from inferfabric.platform.probe import pid_on_port
        return pid_on_port(port)

    # ── Scan / Derive ─────────────────────────────────────────────

    def _scan_actual_services(self) -> list[str]:
        """Scan all known model ports and return names of actually-running services."""
        actual = []
        for name, m in self._models.items():
            status = self._health.check_model(m)
            if status in ("✅", "⏳"):
                actual.append(name)
        return actual

    def _scan_port_owners(self) -> dict:
        """Port-based cross-check: map of model_name → (port, pid) for GPU-bound
        models whose port has a live process (via fuser).

        Catches running services that a health-check miss (transient 503/timeout
        false negatives) would hide. Used by the switch occupancy guard so a
        vLLM that owns its port blocks a new exclusive deploy even when /health
        is unreachable.
        """
        owners = {}
        for name, m in self._models.items():
            if getattr(m, "is_gpu_none", False):
                continue
            port = getattr(m, "port", None)
            if not port:
                continue
            pid = self._port_pid(port)
            if pid is not None:
                owners[name] = (port, pid)
        return owners

    def _derive_gpu_mode(self, actual_services: list[str]) -> GPUMode:
        """Determine actual gpu_mode from running services (gpu_none services don't count)."""
        actual_gpu_mode = GPUMode.IDLE
        gpu_services = [s for s in actual_services
                        if not ((m := self._models.get(s)) and m.is_gpu_none)]
        if gpu_services:
            for svc_name in gpu_services:
                m = self._models.get(svc_name)
                if m and m.is_exclusive:
                    actual_gpu_mode = GPUMode.EXCLUSIVE
                    break
            if actual_gpu_mode == GPUMode.IDLE:
                actual_gpu_mode = GPUMode.SHARED
        return actual_gpu_mode

    # ── Orphan / Stale PID Detection ──────────────────────────────

    def _detect_orphan_pids(self, actual_services: list[str], actions: list[str]) -> None:
        """P0-4/P0-5: Clean up orphan or stale PID entries for all service types.

        v5.2: Uses EngineAdapter.get_port() / get_pid_state_key() instead of
        hardcoded per-type lambdas. New engine types are auto-discovered.
        """
        from inferfabric.engine_adapter import _adapters, get_adapter
        for adapter_name, adapter_cls in _adapters.items():
            try:
                adapter = get_adapter(adapter_name)
            except Exception:
                continue
            pid_key = adapter.get_pid_state_key()
            if not pid_key:
                continue
            pid = getattr(self._proc, pid_key, None)
            if not pid:
                continue

            # Check if process is still alive
            try:
                # M7: os.kill(pid, 0) probes whether THIS pid is alive.
                # os.killpg(pid, 0) only succeeds when pid is a process-group
                # leader; a live non-leader pid recovered via fuser would be
                # misjudged as dead.
                os.kill(pid, 0)
                is_alive = True
            except ProcessLookupError:
                is_alive = False
            except PermissionError:
                # EPERM → the process exists but is owned by another uid → alive
                is_alive = True

            if not is_alive:
                has_live_service = False
                for svc_name in actual_services:
                    m = self._models.get(svc_name)
                    if m and m.type == adapter_name:
                        port = adapter.get_port(m)
                        if port is not None and self._port_pid(port) is not None:
                            has_live_service = True
                            break
                if not has_live_service:
                    actions.append(f"Orphan {pid_key}={pid} dead — clearing")
                    self.state.set(pid_key, "")

            # Stale: PID alive but no active services
            if pid and not actual_services:
                has_live_service = False
                for name, m in self._models.items():
                    if m.type == adapter_name:
                        port = adapter.get_port(m)
                        if port is not None and self._port_pid(port) is not None:
                            has_live_service = True
                            break
                if not has_live_service:
                    actions.append(f"Stale {pid_key}={pid} with no active services — clearing")
                    self.state.set(pid_key, "")
                else:
                    actions.append(f"{pid_key}={pid} still owns port — keeping (health check false negative)")

    def _restore_dead_pids(self, actual_services: list[str], actions: list[str]) -> None:
        """P0-5: If a service is actually running but PID is not tracked, recover via fuser.

        v5.2: Uses EngineAdapter.get_port() / get_pid_state_key().
        """
        from inferfabric.engine_adapter import _adapters, get_adapter
        for adapter_name, adapter_cls in _adapters.items():
            try:
                adapter = get_adapter(adapter_name)
            except Exception:
                continue
            pid_key = adapter.get_pid_state_key()
            if not pid_key:
                continue
            pid = getattr(self._proc, pid_key, None)
            if pid:
                continue  # already tracked

            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.type == adapter_name:
                    port = adapter.get_port(m)
                    if port is not None:
                        recovered_pid = self._port_pid(port)
                        if recovered_pid is not None:
                            self.state.set(pid_key, str(recovered_pid))
                            actions.append(f"Recovered {pid_key}={recovered_pid} for {svc_name} via fuser")
                            break

    # ── VRAM ──────────────────────────────────────────────────────

    def _get_current_vram_pct(self) -> float | None:
        """Current VRAM occupancy pct; None when the reading is unavailable.

        DeviceTelemetry T3: callers must fail closed on None (never treat
        as 0%).
        """
        try:
            snap = (self._device_telemetry.snapshot()
                    if self._device_telemetry is not None else None)
            if snap is None:
                from .health import gpu_used_mb, gpu_total_mb
                used, total = gpu_used_mb(), gpu_total_mb()
            else:
                used, total = snap.memory_used_mb, snap.memory_total_mb
            if used is None or total is None:
                return None
            return used / max(total, 1) * 100
        except Exception:
            return None

    # ── Config Drift ──────────────────────────────────────────────

    def _check_model_config_changed(self, model: ModelConfig) -> bool:
        """Compare stored config hash against current YAML. Returns True if drifted.

        Delegates to config_watcher.detect_drift().
        """
        try:
            return _detect_drift(model, self.state)
        except Exception:
            log.debug("Config hash check failed for %s: %s", model.name, sys.exc_info()[1])
            return False  # conservative: don't restart on check failure

    # ── Health / Running State ────────────────────────────────────

    def _is_model_actively_running(self, model: ModelConfig) -> bool:
        """True if this model is currently deployed AND its service is actually live.

        Uses `_check_model_health` (live port probe) rather than trusting the
        `active_services` state DB, which can lag behind reality after crashes
        or manual `pkill`. Falls back to `active_services` membership for models
        without a probeable service port (e.g. ollama-type models served by a
        shared daemon).
        """
        try:
            if self._check_model_health(model):
                return True
        except Exception:
            log.debug("Health check failed for %s: %s", model.name, sys.exc_info()[1])
        # Fallback: state DB says it's active, or it has no probeable port
        return model.name in self.state.get_active_services()

    def _check_model_health(self, model: ModelConfig) -> bool:
        """Check if a specific model's service is healthy via adapter."""
        from inferfabric.engine_adapter import get_adapter
        try:
            adapter = get_adapter(model.type)
            return adapter.check_health(model) == "✅"
        except KeyError:
            return self._health.check_model(model) == "✅"

    # ── Reconcile ─────────────────────────────────────────────────

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies."""
        db_gpu_mode = self.state.gpu_mode
        db_services = self.state.get_active_services()

        actual_services = self._scan_actual_services()
        actual_gpu_mode = self._derive_gpu_mode(actual_services)

        actions: list[str] = []

        # Fix state inconsistencies
        if actual_gpu_mode != db_gpu_mode:
            actions.append(f"DB gpu_mode='{db_gpu_mode}', actual='{actual_gpu_mode}' — updating")
            self.state.gpu_mode = actual_gpu_mode

        if set(actual_services) != set(db_services):
            actions.append(f"DB services={db_services}, actual={actual_services} — updating")
            self.state.set_active_services(actual_services)

        # Fix profile_state
        db_profile_state = self.state.get("profile_state") or "idle"
        if actual_services and db_profile_state != ServiceState.HEALTHY:
            actions.append(f"profile_state was '{db_profile_state}', services running → healthy")
            self.state.set("profile_state", ServiceState.HEALTHY)
        elif not actual_services and db_gpu_mode == GPUMode.IDLE and db_profile_state != ServiceState.IDLE:
            actions.append(f"profile_state was '{db_profile_state}', no services → idle")
            self.state.set("profile_state", ServiceState.IDLE)

        self._detect_orphan_pids(actual_services, actions)
        self._restore_dead_pids(actual_services, actions)

        # P1-2: gpu_mode=IDLE 但 active_services 还有 GPU 服务 → 自动清理
        if actual_gpu_mode == GPUMode.IDLE and self.state.get_active_services():
            gpu_services = [s for s in actual_services
                            if (m := self._models.get(s)) and not m.is_gpu_none]
            if gpu_services:
                actions.append(
                    f'gpu_mode=IDLE but GPU services remain: {gpu_services} — clearing')
                remaining = [s for s in self.state.get_active_services()
                             if self._models.get(s) and self._models[s].is_gpu_none]
                self.state.set('active_services', json.dumps(remaining))

        # Clear stale switching_target to unblock future switches
        switching = self.state.get("switching_target")
        if switching:
            actions.append(f"switching_target was '{switching}' — clearing")
            self.state.set("switching_target", "")

        return {
            "db_gpu_mode": db_gpu_mode,
            "actual_gpu_mode": actual_gpu_mode,
            "db_services": db_services,
            "actual_services": actual_services,
            "actions": actions,
        }

    # ── Cleanup Dead Services ─────────────────────────────────────

    def cleanup_dead_services(self) -> list[str]:
        """Remove dead services from state and reset GPU mode to idle if all gone."""
        active = list(self.state.get_active_services())
        dead_services = []
        for svc_name in active:
            m = self._models.get(svc_name)
            if not m:
                dead_services.append(svc_name)
                continue
            from inferfabric.engine_adapter import get_adapter
            try:
                adapter = get_adapter(m.type)
                health = adapter.check_health(m)
            except KeyError:
                health = self._health.check_model(m)
            if health == "❌":
                dead_services.append(svc_name)

        if dead_services:
            log.warning("Cleanup: removing dead services %s from state", dead_services)
            remaining = [s for s in active if s not in dead_services]
            self.state.set_active_services(remaining)
            if not remaining and self.state.gpu_mode != GPUMode.IDLE:
                log.info("No active services — resetting gpu_mode to idle")
                self.state.gpu_mode = GPUMode.IDLE
        return dead_services

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self) -> dict:
        """Nuclear reset — kill+clear only (Task 2.4 demoted contract).

        优雅停止所有 active 服务已提升到 ModelManager.force_reset()，经
        ModelLifecycle._stop_all_active(include_none=True) 统一 metadata-driven
        路径完成（覆盖 vllm/tts/asr/sglang/llama_cpp/comfyui 全部引擎）。
        本方法只做 kill + clear：force_kill_all 作为 SIGKILL 兜底安全网，
        清掉优雅停止漏掉的任何进程。
        """
        log.info("Force reset")

        self._proc.force_kill_all()

        # P1-6: Clear GPU CUDA state after force kill to eliminate fragmentation
        self._proc.clear_gpu_cuda_state(force=True)

        used = gpu_used_mb(self._device_telemetry)
        # Review fix: gpu_free reflects the WAIT outcome, not the stale
        # pre-wait sample (an early busy reading must not poison the report,
        # an early free reading must not mask a later regression).
        gpu_free = wait_gpu_free(timeout=20, telemetry=self._device_telemetry)
        if not gpu_free:
            log.warning(
                "Device memory not free after force_reset (%s MB used). "
                "GPU CUDA state was cleared but memory is still in use. "
                "Manual reset may be needed: sudo nvidia-smi --gpu-reset",
                used if used is not None else "unknown",
            )

        self._lock.force_clear()

        self.state.set_multi({
            "gpu_mode": GPUMode.IDLE,
            "active_services": json.dumps([]),
            "profile_state": ServiceState.IDLE,
            "switching_target": "",
            "vllm_pid": "",
            "comfyui_pid": "",
            "tts_pid": "",
            "asr_pid": "",
            "sleep_state": "{}",
        })

        return {
            "status": "reset",
            "gpu_mode": GPUMode.IDLE,
            # Fail-closed: wait_gpu_free returns False when the reading is
            # unavailable — never reported free on missing data.
            "gpu_free": gpu_free,
        }