"""
inferfabric/model_lifecycle.py — Model lifecycle management (extracted from manager.py v4.0).

Responsible for: starting/stopping models (vLLM/ComfyUI/Ollama/OllamaCpp),
deployment flow, shared service add, sleep/wake, independent model management.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .config import (
    GPU_AUTO_CLEAR_CUDA_STATE,
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    ModelConfig,
    load_models,
)
from .gpu_state import GpuStateMachine
from .health import (
    gpu_used_mb,
    gpu_total_mb,
)
from .state import GPUMode, ServiceState, StateDB, validate_transition

log = logging.getLogger("inferfabric")


class ModelLifecycle:
    """Model lifecycle management — deploy, stop, sleep, wake, independent services.

    Owns the concrete operations for each model type. Does NOT hold the orchestration
    logic (that lives in ModelManager switch()). Does NOT hold reconciliation logic
    (that lives in GpuStateMachine).
    """

    def __init__(self, state, proc, health, lock, gpu_state, models, models_dir=None):
        self.state = state
        self._proc = proc
        self._health = health
        self._lock = lock
        self._gpu_state = gpu_state
        self._models = models
        self.models_dir = models_dir

    # ── Start Model (dispatch) ────────────────────────────────────

    def _engine_already_running(self, model: ModelConfig) -> bool:
        """PR-20 ②（评审修复 3）：本模型自己的引擎是否已在跑？

        非变更性探测（proc.engine_exists，见 process_manager/facade.py），
        用于新分配守卫之前的现实识别：proxy 重启后幸存的引擎容器自己
        占着 VRAM，若照走 CUDA clear + VRAM 预算守卫，会把「自己」当
        「外来占用」拒之门外 —— 收养路径永远到不了 start_ninfer。

        契约：proc.engine_exists 返回字面 True 才算在场（`is True` 判定，
        防 MagicMock 替身 truthy 误跳守卫）；探测异常按不在场处理。
        """
        try:
            return self._proc.engine_exists(model) is True
        except Exception:
            return False

    def _start_model(self, model: ModelConfig) -> dict:
        """Unified model start entry — dispatches by type.

        Single source of truth for how each framework launches a model,
        eliminating the per-branch if/else that was duplicated between
        _deploy_model() and _shared_add_service(). Returns one of:
          {"status": "healthy"/"started"/"ok", ...} on success,
          {"status": "error"/"timeout", ...} on failure.
        """
        from inferfabric.engine_adapter import get_adapter
        try:
            adapter = get_adapter(model.type)
            adapter.set_process_manager(self._proc)
            return adapter.start(model)
        except KeyError:
            return {"status": "error", "message": f"Unknown model type: {model.type}"}

    # ── Deploy ────────────────────────────────────────────────────

    def _deploy_model(self, model: ModelConfig, target_mode: str) -> dict:
        """Deploy a model from idle state."""
        t0 = time.time()

        # Ensure we use the latest YAML (prevents stale config after drift)
        model_name = model.name
        self._models = load_models(self.models_dir)
        model = self._models.get(model_name)
        if model is None:
            return {"status": "error", "message": f"Model {model_name} not found in YAML after reload"}

        # PR-20 ②（评审修复 3）: 现实识别先于新分配守卫。本模型的幸存引擎
        # 在场时跳过 CUDA clear 与 VRAM 预算：收养/等待路径不分配任何东西，
        # 重建路径先 docker stop 释放旧占用再分配 —— 三条路径 VRAM 均自洽。
        # 守卫不因此全局关闭：引擎不在场时照常生效（全新部署语义不变）。
        engine_present = self._engine_already_running(model)

        # P1-6: Clear GPU CUDA state before deploying an exclusive model.
        # After ComfyUI (or any GPU-heavy service) exits, the CUDA driver may
        # retain a fragmented memory map. Clearing it here ensures the new
        # model can allocate large contiguous blocks without hitting CUDA OOM
        # even though nvidia-smi shows sufficient free memory.
        # 幸存引擎在场时跳过 —— gpu-reset 会摧毁加载中/健在的容器（15min 冷启）。
        if (not engine_present and GPU_AUTO_CLEAR_CUDA_STATE
                and model.is_exclusive and target_mode == GPUMode.EXCLUSIVE):
            self._proc.clear_gpu_cuda_state()

        # PR-1: VRAM budget guard — reject if peak would exceed 97% of GPU
        # (95% was too tight: bge-m3 ~480MB + peak_vram_mb leaves <10MB margin)
        if not engine_present and model.gpu_role != "none":
            if model.peak_vram_mb > 0:
                current_vram = gpu_used_mb()
                gpu_total = gpu_total_mb()
                budget = int(gpu_total * 0.97)
                if current_vram + model.peak_vram_mb > budget:
                    msg = (f"VRAM budget exceeded: current {current_vram}MB + "
                           f"peak {model.peak_vram_mb}MB = {current_vram + model.peak_vram_mb}MB "
                           f"> budget {budget}MB (97% of {gpu_total}MB)")
                    log.warning(msg)
                    return {"status": "error", "message": msg}
            elif model.typical_vram_pct > 0:
                # Fallback to legacy pct-based check
                current_pct = self._gpu_state._get_current_vram_pct()
                if current_pct + model.typical_vram_pct > 97:
                    msg = (f"VRAM budget exceeded: current ~{current_pct:.0f}% + "
                           f"~{model.typical_vram_pct}% > 95%")
                    log.warning(msg)
                    return {"status": "error", "message": msg}

        results = {}
        # Preserve gpu_role=none services in active_services (PR-18)
        current_active = self.state.get_active_services()
        none_services = [s for s in current_active
                         if s != model.name
                         and (m := self._models.get(s)) and m.is_gpu_none]
        services_to_start = [model.name] + none_services

        # If shared model, optionally also start ComfyUI
        # V1: shared vLLM models don't auto-start ComfyUI
        # User does: iff switch comfyui separately

        # Start the model — centralized type dispatch (eliminates per-branch forgetting)
        results[model.name] = self._start_model(model)

        # Validate
        failed = False
        for svc, res in results.items():
            if res.get("status") not in ("healthy", "started", "ok"):
                failed = True
                break

        if failed:
            self.state.set("profile_state", ServiceState.ERROR)
            # ── 经统一入口停掉失败模型（metadata-driven，adapter 按 deployment
            #    分派，无 per-engine 内联 docker/pkill 或引擎特定端口清理）──
            self._stop_model_process(model, model.name)
            # PID 清理兜底（stop_all 为薄调用，降级见 Task 2.3）
            self._proc.stop_all(active_services=list(self.state.get_active_services()))
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "active_services": json.dumps([]),
                "vllm_pid": "",
                "comfyui_pid": "",
                "sglang_pid": "",
                "sglang_container": "",
                "tts_pid": "",
                "asr_pid": "",
            })
            elapsed = round(time.time() - t0, 1)
            return {
                "status": "error",
                "message": f"Failed to start {model.name}: {results}",
                "results": results,
            }

        # Success
        elapsed = round(time.time() - t0, 1)
        # Record config hashes for drift detection on next switch
        config_hashes = {}
        for svc_name in services_to_start:
            svc_model = self._models.get(svc_name)
            if svc_model:
                config_hashes[f"config_hash:{svc_name}"] = svc_model.config_hash()
        self.state.set_multi({
            "gpu_mode": target_mode,
            "active_services": json.dumps(services_to_start),
            "profile_state": ServiceState.HEALTHY,
            **config_hashes,
        })

        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": target_mode,
            "elapsed_sec": elapsed,
            "results": results,
        }

    # ── Shared Add Service ────────────────────────────────────────

    def _shared_add_service(self, model: ModelConfig) -> dict:
        """Add a shared-mode service. Caller must hold self._lock (see switch()).

        Only starts the new model. Existing shared services remain running.
        Checks typical VRAM headroom before starting.
        """
        if not self._lock.is_held:
            raise RuntimeError("_shared_add_service called without holding GPU lock")
        t0 = time.time()

        if model.name in self.state.get_active_services():
            return {
                "status": "already_active",
                "model": model.name,
                "gpu_mode": GPUMode.SHARED,
            }

        # ── VRAM headroom check (unified with _deploy_model peak_vram_mb guard) ──
        # PR-20 ②（评审修复 3）: 幸存引擎在场 → 跳过新分配守卫（同 _deploy_model，
        # peak 与 pct 两个分支一起跳 —— 守卫测的是「新分配叠加坡上」，在场引擎
        # 走收养/等待/先停后建，不构成叠加）
        if not self._engine_already_running(model):
            if model.peak_vram_mb > 0:
                current_vram = gpu_used_mb()
                gpu_total = gpu_total_mb()
                budget = int(gpu_total * 0.97)
                if current_vram + model.peak_vram_mb > budget:
                    return {
                        "status": "error",
                        "message": (
                            f"VRAM budget exceeded: current {current_vram}MB + "
                            f"peak {model.peak_vram_mb}MB = {current_vram + model.peak_vram_mb}MB "
                            f"> budget {budget}MB (97% of {gpu_total}MB)"
                        ),
                    }
            elif model.typical_vram_pct > 0:
                # Fallback to legacy pct-based check for models without peak_vram_mb
                current_pct = self._gpu_state._get_current_vram_pct()
                if current_pct + model.typical_vram_pct > 97:
                    return {
                        "status": "error",
                        "message": (
                            f"Insufficient GPU memory: current ~{current_pct:.0f}%, "
                            f"{model.name} needs ~{model.typical_vram_pct}%, "
                            f"total would be ~{current_pct + model.typical_vram_pct:.0f}% (limit 95%)."
                        ),
                    }

        # ── Start only the new service — centralized type dispatch ──
        log.info("Shared add (incremental): %s", model.name)
        results = {model.name: self._start_model(model)}

        # Validate
        failed = [k for k, r in results.items() if r.get("status") not in ("healthy", "started", "ok")]
        if failed:
            self.state.set("profile_state", ServiceState.ERROR)
            return {"status": "error", "message": f"Failed to start: {failed}", "results": results}

        # Update state: add to active services + record config hash
        remaining = list(self.state.get_active_services())
        remaining.append(model.name)
        self.state.set_active_services(remaining)
        self.state.set(f"config_hash:{model.name}", model.config_hash())
        self.state.set("profile_state", ServiceState.HEALTHY)

        elapsed = round(time.time() - t0, 1)
        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": GPUMode.SHARED,
            "elapsed_sec": elapsed,
            "active_services": remaining,
            "results": results,
        }

    # ── Common Process-Stop Dispatch ──────────────────────────────

    def _stop_model_process(self, model: ModelConfig, name: str) -> None:
        """Dispatch process stop by model type. Shared helper for stop_service and stop_independent."""
        from inferfabric.engine_adapter import get_adapter
        try:
            adapter = get_adapter(model.type)
            adapter.set_process_manager(self._proc)
            adapter.stop(model)
        except KeyError:
            log.warning("Unknown model type %s — cannot stop", model.type)

    def _stop_all_active(self, services: list[str] | None = None,
                         include_none: bool = False) -> None:
        """经 _stop_model_process 停所有 active 服务（adapter 按 deployment 分派）。

        默认只停 needs_gpu 服务（_switch_to_idle/_deploy_model 用——
        gpu_role=none 服务不影响 GPU，予以保留）。
        include_none=True 时也停 gpu_role=none 服务（force_reset 用——
        替代手动 ollama_cpp/asr 停止循环）。
        """
        svcs = services or list(self.state.get_active_services())
        for svc_name in svcs:
            m = self._models.get(svc_name)
            if m and (m.needs_gpu or include_none):
                self._stop_model_process(m, svc_name)

    # ── Switch (internal helpers) ─────────────────────────────────

    def _switch_exclusive(self, model: ModelConfig) -> dict:
        """Switch from one exclusive model to another (same-port swap).

        Stops the currently active exclusive model, clears active_services,
        then deploys the new one. If deployment fails, attempts rollback to the
        previous model to avoid service interruption.
        """
        # Record old model info for rollback
        current = list(self.state.get_active_services())
        old_models = {svc: self._models.get(svc) for svc in current}

        # Stop current active services, but preserve gpu_role=none (PR-18)
        preserved_none = []
        for svc_name in current:
            svc_model = old_models.get(svc_name)
            if svc_model and svc_model.is_gpu_none:
                log.info("Preserving gpu_role=none service: %s", svc_name)
                preserved_none.append(svc_name)
                continue
            log.info("Stopping current exclusive service: %s", svc_name)
            if svc_model:
                self._stop_model_process(svc_model, svc_name)
            else:
                log.warning("No model config for exclusive service %s, skipping stop", svc_name)

        # Clear GPU-bound services before deploying new model (none services preserved above)
        self.state.set("active_services", json.dumps(preserved_none))

        # P1-6: Clear GPU CUDA state between exclusive model switches
        # After the old model's process exits, the CUDA driver may retain
        # fragmented memory. Clearing here prevents allocation failures
        # when the new model tries to allocate its KV cache.
        if GPU_AUTO_CLEAR_CUDA_STATE:
            self._proc.clear_gpu_cuda_state()

        # Deploy the new model
        result = self._deploy_model(model, GPUMode.EXCLUSIVE)

        # If deployment failed, attempt rollback to old model
        if result.get("status") not in ("switched", "already_active"):
            log.warning("Deploy of %s failed, attempting rollback to %s", model.name, list(old_models.keys()))
            rollback_ok = False
            rollback_svc = None
            for svc_name, svc_model in old_models.items():
                if svc_model and svc_model.gpu_role != "none":
                    # Reload YAML to get fresh config for rollback
                    self._models = load_models(self.models_dir)
                    fresh_model = self._models.get(svc_name)
                    if fresh_model:
                        rb = self._deploy_model(fresh_model, fresh_model.gpu_role)
                        if rb.get("status") == "switched":
                            rollback_ok = True
                            rollback_svc = svc_name
                            log.info("Rollback to %s succeeded", svc_name)
                            break
            if rollback_ok:
                # Clean result — caller gets a coherent rollback-success dict
                result = {"status": "switched", "model": rollback_svc, "rollback": "succeeded"}
            else:
                self.state.set_multi({
                    "gpu_mode": GPUMode.IDLE,
                    "profile_state": ServiceState.ERROR,
                })
                log.error("Rollback failed — system in ERROR state, no model available")
                result["rollback"] = "failed"

        return result

    def _switch_independent(self, model: ModelConfig) -> dict:
        """Switch to a GPU-independent model — does NOT change the GPU state machine.

        GPU-independent models (gpu_role == "none"):
          - Do not change self.gpu_mode
          - May coexist with any GPU mode (exclusive/shared/other none)
          - Launch the model process directly — framework determined by type
        """
        t0 = time.time()
        # GPU-independent models (cpu-only) can coexist with any GPU mode.

        # Reload YAML to detect config drift against disk (parity with GPU-bound path)
        model_name = model.name
        self._models = load_models(self.models_dir)
        model = self._models.get(model_name)
        if model is None:
            return {"status": "error", "message": f"Model {model_name} not found in YAML after reload"}

        # Start the model (centralized type dispatch)
        result = self._start_model(model)
        if result.get("status") not in ("healthy", "started", "ok"):
            return result

        # Update active_services + record config hash (gpu_mode stays unchanged)
        remaining = list(self.state.get_active_services())
        if model.name not in remaining:
            remaining.append(model.name)
        self.state.set_active_services(remaining)
        self.state.set(f"config_hash:{model.name}", model.config_hash())
        self.state.set("profile_state", ServiceState.HEALTHY)

        elapsed = round(time.time() - t0, 1)
        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": self.state.gpu_mode,  # unchanged
            "elapsed_sec": elapsed,
            "results": {model.name: result},
        }

    def _switch_to_idle(self) -> dict:
        # v5.6.8 debug: log stack trace on every switch-to-idle to trace mysterious caller
        import traceback
        log.info("_switch_to_idle() caller:\n%s", "".join(traceback.format_stack()[:-1]))

        """Stop all services and transition to idle.  Captures service list
        before reconcile to avoid the gpu_mode/active_services drift where
        reconcile sets mode=IDLE but services are still in active_services.
        """
        current_mode = self.state.gpu_mode

        # Capture services BEFORE reconcile — reconcile may clear gpu_mode
        # without clearing active_services, creating a state inconsistency.
        from_services_pre = list(self.state.get_active_services())
        if current_mode == GPUMode.IDLE and not from_services_pre:
            return {"status": "already_active", "model": "idle"}

        log.info("Reconciling state before idle switch")
        self._gpu_state.reconcile()

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        # Re-read after reconcile, but keep pre-reconcile list as fallback
        from_services = list(self.state.get_active_services())
        log.info("Switch to idle from %s (gpu_mode=%s)", from_services, self.state.gpu_mode)

        try:
            # ── 经统一入口停止所有 GPU-bound 服务（adapter 按 deployment 分派）──
            # include_none=False（默认）：gpu_role=none 服务保留，由 set_multi 过滤
            self._stop_all_active(services=from_services)
            # PID 清理 + GPU 兜底（stop_all 降级为薄方法，见 Task 2.3）
            self._proc.stop_all(active_services=from_services)

            gpu_idle = self._proc._wait_gpu_idle(timeout=30)
            if gpu_idle.get("status") not in ("ok", "force"):
                self._proc.force_kill_all()
                gpu_idle2 = self._proc._wait_gpu_idle(timeout=15)
                if gpu_idle2.get("status") not in ("ok", "force"):
                    self.state.set("profile_state", ServiceState.ERROR)
                    return {"status": "error", "message": "GPU not freed after force kill"}

            # P1-6: Clear GPU CUDA state to eliminate fragmentation
            # after any GPU-bound service stops (ComfyUI in particular)
            if GPU_AUTO_CLEAR_CUDA_STATE:
                if any(m.is_comfyui or m.is_vllm or m.is_sglang
                       for svc in from_services if (m := self._models.get(svc))):
                    # only clear if GPU had a heavy service running
                    self._proc.clear_gpu_cuda_state()

            # Update state
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "active_services": json.dumps([s for s in from_services
                                               if (m := self._models.get(s)) and m.is_gpu_none]),
                "vllm_pid": "",
                "comfyui_pid": "",
                "tts_pid": "",
                "asr_pid": "",
                "profile_state": ServiceState.IDLE,
            })

            elapsed = round(time.time() - t0, 1)
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, "idle", elapsed, "ok")

            return {
                "status": "switched",
                "model": "idle",
                "elapsed_sec": elapsed,
                "stopped": from_services,
            }
        except Exception as e:
            self.state.set("profile_state", ServiceState.ERROR)
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    # ── Stop Single Service ───────────────────────────────────────

    def stop_service(self, name: str) -> dict:
        """Stop a running service.

        - shared: stops just that service; if last shared, auto-transitions to idle.
        - exclusive: delegates to _switch_to_idle (single GPU-bound service ⇒
          stopping it = releasing the GPU). Returns status='stopped' (normalized
          from _switch_to_idle's 'switched' so CLI/handler see stopped).
        - gpu_role=none: routed to stop_independent (no GPU-state change).
        Verifies GPU memory is actually freed (catches orphaned processes).
        """
        if name not in self.state.get_active_services():
            return {"status": "error", "message": f"Service '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        # gpu-none models use stop_independent (not blocked by exclusive GPU)
        if model.is_gpu_none:
            return self.stop_independent(name)

        # exclusive 模型：单 GPU 上停它 = 释放整个 GPU → 转 idle。
        # active_services 在 exclusive 下恰为该模型，转 idle 等价停它。
        # 返回值归一为 stopped（CLI cli.py:147 / handler _handle_stop 依赖）。
        if self.state.gpu_mode == GPUMode.EXCLUSIVE:
            result = self._switch_to_idle()
            if result.get("status") == "switched":
                return {
                    "status": "stopped",
                    "model": name,
                    "gpu_mode": GPUMode.IDLE,
                    "stopped": result.get("stopped", [name]),
                    "elapsed_sec": result.get("elapsed_sec"),
                }
            return result  # error 透传

        # Stop the specific service (pass port for port-based cleanup)
        self._stop_model_process(model, name)

        # Verify GPU actually freed — catch orphaned processes (skip CPU-only models)
        if model.needs_gpu:
            gpu_idle = self._proc._wait_gpu_idle(timeout=20)
            if gpu_idle.get("status") not in ("ok", "force"):
                log.warning("GPU not freed after stop %s — force-kill target port only", name)
                # Targeted cleanup: only this model's port, not force_kill_all
                port = getattr(model, 'port', None)
                if port:
                    self._proc._pkill_by_port(port)
                else:
                    self._stop_model_process(model, name)
                self._proc._wait_gpu_idle(timeout=15)

        # Update active services
        remaining = [s for s in self.state.get_active_services() if s != name]
        self.state.set_active_services(remaining)

        # Auto-transition to idle if no services left
        if not remaining:
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "profile_state": ServiceState.IDLE,
            })
            return {
                "status": "stopped",
                "model": name,
                "gpu_mode": GPUMode.IDLE,
                "message": f"Stopped {name}. No services remaining → idle.",
            }

        return {
            "status": "stopped",
            "model": name,
            "gpu_mode": GPUMode.SHARED,
            "remaining": remaining,
        }

    # ── Independent Model Management (gpu_role: none) ──────────────

    def stop_independent(self, name: str) -> dict:
        """Stop a GPU-independent model (gpu_role: none).

        Unlike stop_service(), this method:
        - Only accepts models with gpu_role == "none"
        - Does NOT change the GPU mode (idle/exclusive/shared tri-state)
        - Does NOT auto-transition to idle when last service is removed

        Use stop_service() for GPU-bound models (exclusive/shared).
        """
        if name not in self.state.get_active_services():
            return {"status": "error", "message": f"Independent model '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        if not model.is_gpu_none:
            return {"status": "error", "message": f"Model '{name}' is not an independent model (gpu_role={model.gpu_role})"}

        # Stop the process — dispatch by type (shared helper)
        self._stop_model_process(model, name)

        # Remove from active_services (gpu_mode stays unchanged)
        remaining = [s for s in self.state.get_active_services() if s != name]
        self.state.set_active_services(remaining)

        return {"status": "stopped", "model": name, "gpu_mode": self.state.gpu_mode}

    def list_independent(self) -> list[str]:
        """Return names of currently running GPU-independent models (gpu_role: none)."""
        return [name for name in self.state.get_active_services()
                if (m := self._models.get(name)) and m.is_gpu_none]

    # ── Sleep / Wake (L2 only) ────────────────────────────────────

    def sleep_model(self, name: str) -> dict:
        """Put a running vLLM model to L2 sleep.

        Rules:
        - Only one model may sleep at a time.
        - Exclusive model sleeping → GPU transitions to idle (VRAM freed).
        - Shared model sleeping → GPU stays shared (other services unaffected).
        """
        if name not in self.state.get_active_services():
            return {"status": "error", "message": f"Model '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}
        if not model.is_vllm:
            return {"status": "error", "message": f"Model '{name}' is not a vLLM model"}

        sleep_cfg = model.vllm.sleep_mode
        if not sleep_cfg or not sleep_cfg.enabled:
            return {"status": "error", "message": f"Sleep mode not enabled for '{name}'"}

        # Check if already sleeping
        if self.state.get_sleep_state(name):
            return {"status": "already_sleeping", "model": name}

        # Mutex: only one model sleeping at a time
        all_sleep = self.state.get_all_sleep_states()
        if all_sleep:
            existing = list(all_sleep.keys())[0]
            return {"status": "error",
                    "message": f"Model '{existing}' is already sleeping. Wake or stop it first."}

        log.info("Sleeping model '%s' (L2)", name)
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter(model.type)
        adapter.set_process_manager(self._proc)
        result = adapter.sleep(model)

        if result["status"] == "ok":
            self.state.set_sleep_state(name, 2)

            # Exclusive sleeping → GPU → idle (VRAM freed)
            if model.is_exclusive:
                self.state.set_multi({
                    "gpu_mode": GPUMode.IDLE,
                    "profile_state": ServiceState.IDLE,
                })

            log.info("Model '%s' is now sleeping (L2), GPU=%s", name,
                     "idle" if model.is_exclusive else "shared")
        else:
            self.state.set_sleep_state(name, None)

        return {**result, "model": name}

    def wake_model(self, name: str) -> dict:
        """Wake a sleeping vLLM model.

        Rules:
        - Exclusive model: GPU must be idle → wake → GPU → exclusive.
        - Shared model: GPU must be idle or shared → wake → GPU → shared.
        """
        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}
        if not model.is_vllm:
            return {"status": "error", "message": f"Model '{name}' is not a vLLM model"}

        sleep_state = self.state.get_sleep_state(name)
        if not sleep_state:
            return {"status": "already_awake", "model": name, "message": "Model is not sleeping"}

        # Validate GPU mode for wake — use shared transition table
        current_gpu = self.state.gpu_mode
        target_mode = model.gpu_role  # 'exclusive' or 'shared'
        if not validate_transition(current_gpu, target_mode):
            return {"status": "error",
                    "message": f"Cannot wake model '{name}': GPU is {current_gpu}, cannot transition to {target_mode}"}

        log.info("Waking model '%s' (L2)", name)
        from inferfabric.engine_adapter import get_adapter
        adapter = get_adapter(model.type)
        adapter.set_process_manager(self._proc)
        result = adapter.wake(model)

        if result["status"] == "ok" or result["status"] == "killed_for_restart":
            self.state.set_sleep_state(name, None)

            if model.is_exclusive:
                self.state.set_multi({
                    "gpu_mode": GPUMode.EXCLUSIVE,
                    "profile_state": ServiceState.HEALTHY,
                })
                current_services = self.state.get_active_services()
                if name not in current_services:
                    self.state.add_active_service(name)
            else:
                # Shared model: process was killed, restart via deploy
                if not self._lock.acquire():
                    return {"status": "error", "message": "GPU switch in progress (lock held)"}
                try:
                    if self.state.gpu_mode == GPUMode.IDLE:
                        deploy_result = self._deploy_model(model, GPUMode.SHARED)
                    else:
                        deploy_result = self._shared_add_service(model)
                except Exception as e:
                    deploy_result = {"status": "error", "message": str(e)}
                finally:
                    self._lock.release()
                return deploy_result

            log.info("Model '%s' is now awake", name)

        return {**result, "model": name}