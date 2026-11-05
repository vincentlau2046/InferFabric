"""
inferfabric/manager.py — Model orchestration layer (v4.0, slim facade).

v4.0: Profile concept eliminated. Models are self-describing plugins.
Tri-state GPU mode: idle / exclusive / shared.
Switch rules enforced by validate_transition().

This file is now a thin facade — GPU state machine logic lives in
gpu_state.py, model lifecycle operations live in model_lifecycle.py.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import VLLMConfig, ComfyUIConfig

from .config import (
    MODELS_DIR,
    DEFAULT_STATE_DB,
    ModelConfig,
    load_models,
)
from .state import StateDB, ServiceState, GPUMode, validate_transition
from .gpu_lock import GPULock
from .process_manager import ProcessManager
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    check_http_status,
)
from .health_checker import DefaultHealthChecker
from .interfaces import IStateDB, IProcessManager, IHealthChecker, IGPULock
from .model_discovery import discover_local_models as _discover_local_models
from .model_discovery import auto_deploy as _auto_deploy
from .gpu_state import GpuStateMachine
from .model_lifecycle import ModelLifecycle

log = logging.getLogger("inferfabric")


class ModelManager:
    """Orchestrates GPU resource allocation via model plugins.

    GPU Mode State Machine:
      idle → exclusive: deploy exclusive model, GPU fully locked
      idle → shared:    deploy shared model/service
      exclusive → idle:  stop exclusive model
      shared → idle:     stop all shared services
      shared → shared:   add/remove shared service (incremental hot-plug;
               other shared services keep running)

      ❌ exclusive → shared:  must idle first
      ❌ shared → exclusive:  must idle first
    """

    def __init__(
        self,
        models_dir: str | Path = str(MODELS_DIR),
        state_db_path: str | Path = str(DEFAULT_STATE_DB),
        proc: IProcessManager | None = None,
        state: IStateDB | None = None,
        lock: IGPULock | None = None,
        health: IHealthChecker | None = None,
    ):
        """Initialize ModelManager.

        All dependency parameters are optional — when omitted the concrete
        implementations are used (runtime zero-overhead structural typing).
        """
        self.models_dir = Path(models_dir)
        self.state = state if state is not None else StateDB(Path(state_db_path))
        self._lock = lock if lock is not None else GPULock()
        self._proc = proc if proc is not None else ProcessManager(self.state)
        # 状态机感知重启钩子（NInferAdapter.restart）依赖 proc.mgr 回引用；
        # 缺失时适配器会静默退回基类 stop+start（绕过 503 守卫/历史/状态同步）。
        try:
            self._proc.mgr = self
        except (AttributeError, TypeError):
            pass  # 测试桩/受限 IProcessManager 不接受回引用 → 保持基类回退
        self._health = health if health is not None else DefaultHealthChecker()
        self._models = load_models(self.models_dir)
        # 计算属性: gpu_mode 从 active_services 实时推导
        self.state.set_models_lookup(self.get_model)

        # Sub-module instances
        self._gpu_state = GpuStateMachine(
            self.state, self._proc, self._health, self._lock, self._models,
        )
        self._lifecycle = ModelLifecycle(
            self.state, self._proc, self._health, self._lock,
            self._gpu_state, self._models, self.models_dir,
        )

        # Startup self-check: catch missing cross-layer methods early
        # (e.g. reload_models, start_ollama — P0 regressions from audit)
        for method in ("reload_models", "auto_deploy", "switch", "stop_service",
                       "sleep_model", "wake_model", "reconcile", "force_reset"):
            if not callable(getattr(self, method, None)):
                raise RuntimeError(
                    f"ModelManager missing required method: {method}()"
                )
        for method in ("start_ollama", "run_ollama", "start_vllm", "stop_vllm",
                       "start_sglang", "stop_sglang"):
            if not callable(getattr(self._proc, method, None)):
                raise RuntimeError(
                    f"ProcessManager missing required method: {method}()"
                )

    # ── Properties (thin) ─────────────────────────────────────────

    @property
    def gpu_mode(self) -> str:
        return self.state.gpu_mode

    @property
    def active_services(self) -> list[str]:
        return self.state.get_active_services()

    @property
    def current_service(self) -> Optional[str]:
        """For backward compat — returns first active service or 'idle'."""
        services = self.active_services
        return services[0] if services else "idle"

    @property
    def models_d(self) -> dict:
        """Backward compat — returns internal _models dict."""
        return self._models

    # ── Model Lookup ─────────────────────────────────────────────

    def get_model(self, name: str) -> Optional[ModelConfig]:
        """Get model config by name."""
        return self._models.get(name)

    def list_models(self) -> list[dict]:
        """List all available models from models.d/. Skips ollama_daemon entries."""
        def _get_context(m):
            """Get context window via engine adapter."""
            from inferfabric.engine_adapter import get_adapter
            try:
                adapter = get_adapter(m.type)
                return adapter.get_context_window(m)
            except KeyError:
                return None

        def _engine_config(m):
            """Extract engine operational params from model config."""
            import re as _re
            if m.type == 'vllm' and m.vllm:
                v = m.vllm
                extra = v.extra_flags or ''
                bt = None; cp = False
                mt = _re.search(r'--max-num-batched-tokens[= ](\d+)', extra)
                if mt: bt = int(mt.group(1))
                if '--enable-chunked-prefill' in extra: cp = True
                return {
                    'max_num_seqs': v.max_num_seqs,
                    'gpu_memory_utilization': v.gpu_memory_utilization,
                    'kv_cache_dtype': v.kv_cache_dtype,
                    'max_batched_tokens': bt,
                    'chunked_prefill': cp,
                    'kv_offloading_size': v.kv_offloading_size,
                    'conda_env': v.conda_env,
                    'served_name': v.served_name,
                }
            if m.type == 'sglang' and m.sglang:
                s = m.sglang
                return {
                    'max_running_requests': s.max_running_requests,
                    'context_length': s.context_length,
                    'mem_fraction': s.mem_fraction,
                    'cpu_offload_gb': s.cpu_offload_gb,
                    'enable_lmcache': s.enable_lmcache,
                    'served_name': s.served_name,
                }
            if m.type in ('llama_cpp', 'ollama') and m.llama_cpp:
                o = m.llama_cpp
                return {
                    'threads': o.threads,
                    'context_size': o.context_size,
                    'gpu_layers': o.gpu_layers,
                    'served_name': m.name,
                }
            if m.type == 'comfyui' and m.comfyui:
                return {
                    'conda_env': m.comfyui.conda_env,
                    'working_dir': getattr(m.comfyui, 'working_dir', ''),
                    'served_name': m.name,
                }
            if m.type == 'ollama' and m.ollama:  # inherited daemon — sparse info
                return {
                    'model_ref': getattr(m.ollama, 'model_ref', '') or '',
                    'served_name': m.ollama.model_ref if m.ollama and m.ollama.model_ref else m.name,
                }
            return None
        return [
            {
                'name': m.name,
                'description': m.description,
                'mode': m.gpu_role,
                'type': m.type,
                'active': self._gpu_state._is_model_actively_running(m),
                'model_type': getattr(m, 'model_type', 'llm'),
                'modality': getattr(m, 'resolved_modality', 'text'),
                'quantization': getattr(m, 'quantization', ''),
                'context_window': _get_context(m),
                'engine_config': _engine_config(m),
            }
            for m in self._models.values()
            if m.type != 'ollama_daemon'
        ]

    def find_model_by_served_name(self, served_name: str) -> Optional[ModelConfig]:
        """Find model by its served_name across all backend types (for proxy routing)."""
        for m in self._models.values():
            if m.served_name == served_name:
                return m
        # Fallback: match by model name
        for m in self._models.values():
            if m.name == served_name:
                return m
        return None

    # ── Config Reload ─────────────────────────────────────────────

    def reload_models(self):
        """Reload models.d/*.yaml and propagate to all dependent sub-modules.

        Thread-safe with respect to concurrent switch(): Python dict assignment
        is atomic, and the switching_target gate in ensure_service / switch()
        prevents concurrent reload + deploy races.
        """
        self._models = load_models(self.models_dir)
        self._gpu_state._models = self._models
        self._lifecycle._models = self._models
        self.state.set_models_lookup(self.get_model)
        try:
            from inferfabric.dashboard import invalidate_cache
            invalidate_cache()
        except Exception:
            pass
        log.info("Models reloaded: %d from %s", len(self._models), self.models_dir)

    # ── Health Check (tri-state) ─────────────────────────────────

    def check_vllm_health(self, port: int) -> str:
        return check_http_status(f"http://localhost:{port}/health")

    def check_comfyui_health(self, url: str) -> str:
        return check_http_status(url)

    def check_http_health(self, port: int, path: str = "/health") -> str:
        """Generic HTTP health check for any backend."""
        return check_http_status(f"http://localhost:{port}{path}")

    def check_ollama_health(self, port: int = 11434) -> str:
        """Check if Ollama daemon is running."""
        return check_http_status(f"http://localhost:{port}/api/tags")

    # ── Switch ────────────────────────────────────────────────────

    def switch(self, target: str) -> dict:
        # v5.6.8 debug: log stack trace on every switch to trace mysterious caller
        import traceback
        log.info("switch(%r) caller:\n%s", target, "".join(traceback.format_stack()[:-1]))

        """Switch to target model/service.

        Two orthogonal paths dispatched by gpu_role:
          - gpu_role == "none"  → _switch_independent() — does NOT touch the
            GPU state machine (can coexist with idle/exclusive/shared).
          - gpu_role != "none" → GPU state machine path, enforcing tri-state:
              idle → exclusive/shared: allowed
              exclusive → idle: allowed
              shared → idle: allowed
              shared → shared: allowed (incremental: add via _shared_add_service /
               remove via stop_service — existing shared services keep running)
              exclusive → exclusive: allowed (same-port swap)
              exclusive → shared:    ❌ must idle first
              shared → exclusive:    ❌ must idle first
        """
        # ── Handle idle ──────────────────────────────────────────────
        if target == "idle":
            return self._lifecycle._switch_to_idle()

        # ── Model lookup & already-running check ──────────────────────
        # D1 启动即读：非活跃模型的部署参数一律取「YAML + 应用层」磁盘当前值
        # （长驻代理进程的内存注册表可能落后于 CLI/别处的 tune apply）。
        # 活跃模型不重读——其配置漂移由下方 vLLM drift 检查负责。
        if target not in self.active_services:
            try:
                self._models = load_models(self.models_dir)
            except Exception as e:
                # D1 降级：磁盘读失败时沿用内存注册表（核心 switch 不因此阻塞），
                # 但可能部署旧场景 → error 级可见，便于排查 D1 保证失效
                log.error("switch(%s): 重读模型配置失败（D1 降级：沿用内存注册表，可能部署旧场景）: %s",
                          target, e)
        model = self._models.get(target)
        if not model:
            return {"status": "error", "message": f"Unknown model: {target}. Available: {list(self._models.keys())}"}

        # Already running? (P0-1: check config drift before skipping)
        if target in self.active_services:
            if model.is_vllm and model.vllm:
                # Reload YAML to detect config drift against disk
                self._models = load_models(self.models_dir)
                model = self._models.get(target)
                if model is None:
                    log.warning("YAML for %s not found after reload — skipping drift check", target)
                    return {"status": "already_active", "model": target}
                changed = self._gpu_state._check_model_config_changed(model)
                if changed:
                    log.info("Config changed for %s — restarting", target)
                    self._lifecycle._switch_to_idle()
                else:
                    return {"status": "already_active", "model": target}
            else:
                return {"status": "already_active", "model": target}

        # ── Dispatch by gpu_role ────────────────────────────────────
        # Path A: GPU-independent models — do not touch the GPU state machine.
        if model.gpu_role == "none":
            return self._lifecycle._switch_independent(model)

        # Path B: GPU-bound models — go through the tri-state GPU state machine.
        target_mode = model.gpu_role  # 'exclusive' or 'shared'
        current_mode = self.gpu_mode

        # ── Validate GPU mode transition ──────────────────────────────
        # Validate transition — allow exclusive → exclusive (same-port swap)
        if not validate_transition(current_mode, target_mode):
            # Allow exclusive → exclusive: stop old, start new
            if current_mode == GPUMode.EXCLUSIVE and target_mode == GPUMode.EXCLUSIVE:
                pass  # Allowed below
            elif current_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in exclusive mode ({self.active_services[0] if self.active_services else 'unknown'} running). "
                               f"Run 'iff switch idle' first.",
                }
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in shared mode ({', '.join(self.active_services) if self.active_services else 'none'} running). "
                               f"Run 'iff switch idle' first to deploy exclusive model.",
                }
            else:
                return {"status": "error", "message": f"Invalid transition: {current_mode} → {target_mode}"}

        # ── GPU Occupancy Guard ─────────────────────────────────────
        # Prevent DB inconsistency bypass: if DB says idle but GPU
        # has running gpu_role≠none services (e.g., vLLM survived an
        # error cleanup), block the switch rather than double-deploying.
        if current_mode == GPUMode.IDLE and target != "idle":
            actual = self._gpu_state._scan_actual_services()
            gpu_services = [s for s in actual
                            if (m := self._models.get(s)) and not m.is_gpu_none
                            and s != target]
            # Port-based cross-check: catch running services that the health
            # scan missed (transient 503 / timeout false negatives). A vLLM
            # process still owning its port means the GPU is actually busy.
            port_owners = {name: p for name, p in self._gpu_state._scan_port_owners().items()
                           if name != target}
            if gpu_services or port_owners:
                vram = gpu_used_mb()
                occupied = ", ".join(
                    (f"{s} (port {p[0]}, pid {p[1]})" for s, p in port_owners.items()
                    if s not in gpu_services)
                ) or ", ".join(gpu_services)
                return {
                    "status": "error",
                    "message": f"GPU is occupied ({occupied or ', '.join(gpu_services)}, {vram}MB used). "
                               f"DB state is stale (gpu_mode=idle but GPU busy). "
                               f"Run 'iff reconcile' or 'iff reset'.",
                }

        # ── Acquire lock, deploy, and record ──────────────────────────
        # Gate: respect switching_target to prevent concurrent switches
        switching = self.state.get("switching_target") or ""
        if switching and switching != target and target != "idle":
            log.warning("Switch to %s blocked — GPU switching to %s", target, switching)
            return {"status": "error", "message": f"GPU switch in progress ({switching})"}

        # Acquire GPU lock
        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch: %s → %s (gpu_mode: %s → %s)", from_services, target, current_mode, target_mode)

        self.state.set_multi({"profile_state": ServiceState.SWITCHING, "switching_target": target})

        try:
            if current_mode == GPUMode.IDLE:
                # Fresh start — just deploy
                result = self._lifecycle._deploy_model(model, target_mode)
            elif current_mode == GPUMode.EXCLUSIVE and target_mode == GPUMode.EXCLUSIVE:
                # Exclusive → exclusive: stop old, start new (same-port swap)
                result = self._lifecycle._switch_exclusive(model)
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.SHARED:
                # Shared add (incremental) — start only the new model;
                # existing shared services keep running (VRAM budget checked first)
                result = self._lifecycle._shared_add_service(model)
            else:
                result = {"status": "error", "message": f"Unexpected state: {current_mode} → {target_mode}"}

            # Record history
            elapsed = round(time.time() - t0, 1)
            result_status = result.get("status")
            status = "ok" if result_status in ("switched", "already_active") else "error"
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, target, elapsed, status)

            # P2-4: Guard against leaked gpu_mode on non-exception failure.
            if status == "error" and current_mode != GPUMode.IDLE:
                actual = self.state.gpu_mode
                if actual == target_mode:
                    log.warning("Switch failed but gpu_mode leaked as %s — rolling back to %s",
                                actual, current_mode)
                    self.state.set("gpu_mode", current_mode)

            # P2-4: Ensure profile_state is reset on non-exception failure
            # (lifecycle may leave it at SWITCHING if it returned error without setting it).
            if status == "error":
                try:
                    self.state.set("profile_state", ServiceState.ERROR)
                except Exception:
                    log.warning("Failed to set profile_state=ERROR after switch failure")

            return result

        except Exception as e:
            log.exception("Switch failed")
            # P2-4: Roll back gpu_mode to the pre-switch value.
            self.state.set("gpu_mode", current_mode)
            self.state.set("profile_state", ServiceState.ERROR)
            self.state.add_history(",".join(from_services), target, time.time() - t0, "error")
            return {"status": "error", "message": str(e)}
        finally:
            self.state.set("switching_target", "")
            self._lock.release()

    # ── Status ────────────────────────────────────────────────────

    def status(self) -> dict:
        active = list(self.active_services)
        sleep_states = self.state.get_all_sleep_states()
        services_status = {}
        services_info = {}
        dead_services = []
        for svc_name in active:
            m = self._models.get(svc_name)
            if not m:
                dead_services.append(svc_name)
                continue
            info = {"mode": m.gpu_role, "type": m.type}
            if m.port:
                info["port"] = m.port
            if m.vllm and m.vllm.port:
                info["port"] = m.vllm.port
            health = self._health.check_model(m)
            # Append sleep state if applicable
            sleep_label = sleep_states.get(svc_name, "")
            if sleep_label:
                health = f"{health} (sleeping {sleep_label.upper()})"
            services_status[svc_name] = health
            services_info[svc_name] = info
            # Collect dead services (excluded from returned active list)
            if health == "❌":
                dead_services.append(svc_name)

        active_services = [s for s in active if s not in dead_services]

        # D5: 每模型当前场景（直读应用层文件，≡ 容器实际/下次部署意图；
        # 无条目 = "default" = 模型 YAML 当前值）——Dashboard 模型卡徽标用。
        from inferfabric.config import read_applied_scenarios
        applied = read_applied_scenarios()
        scenario_active = {
            m.name: str((applied.get(m.name) or {}).get("active_preset") or "default")
            for m in self._models.values()
        }

        return {
            "gpu_mode": self.gpu_mode,
            "active_services": active_services,
            "services_health": services_status,
            "services_info": services_info,
            "scenario_active": scenario_active,
            "sleep_states": sleep_states,
            "switch_target": self.state.get("switching_target") or None,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "gpu_util_pct": gpu_used_mb() / gpu_total_mb() * 100 if gpu_total_mb() > 0 else 0.0,
            "vllm_pid": self._proc.vllm_pid,
            "comfyui_pid": self._proc.comfyui_pid,
            "tts_pid": self._proc.tts_pid,
            "asr_pid": self._proc.asr_pid,
            "local_services": self._get_local_services_info(),
        }

    # ── Delegation: GpuStateMachine ───────────────────────────────

    def _get_local_services_info(self) -> dict:
        """Collect asr/tts local service endpoints from iff.yaml + model state."""
        result = {}
        # Load iff.yaml directly (runtime config from ProxyManager not accessible here)
        try:
            from inferfabric.config import IFF_DATA_DIR
            import yaml
            cfg_path = IFF_DATA_DIR / "iff.yaml"
            if cfg_path.exists():
                with open(cfg_path) as f:
                    runtime = yaml.safe_load(f) or {}
            else:
                runtime = {}
        except Exception:
            runtime = {}

        for svc_name in ("asr", "tts"):
            svc_cfg = runtime.get(svc_name, {})
            if not svc_cfg or not svc_cfg.get("enabled", False):
                continue
            info = {
                "enabled": True,
                "port": svc_cfg.get("port"),
                "health_url": svc_cfg.get("health_url"),
                "api_base": svc_cfg.get("api_base"),
            }
            # PID from process manager
            pid = self._proc.asr_pid if svc_name == "asr" else self._proc.tts_pid
            info["pid"] = pid
            info["running"] = pid is not None
            # Active from model type matching
            info["active"] = False
            for svc_name_inner in self.active_services:
                m = self._models.get(svc_name_inner)
                if m and (
                    (svc_name == "asr" and m.is_asr_server) or
                    (svc_name == "tts" and m.is_tts_server)
                ):
                    info["active"] = True
                    break
            result[svc_name] = info
        return result

    def reconcile(self) -> dict:
        return self._gpu_state.reconcile()

    def cleanup_dead_services(self) -> list[str]:
        return self._gpu_state.cleanup_dead_services()

    def force_reset(self) -> dict:
        """Nuclear reset: graceful-stop all active services, then kill+clear.

        Task 2.4（关闭 R6 窗口）：先经统一 metadata-driven 路径
        （ModelLifecycle._stop_all_active，adapter 按 deployment 分派）优雅停止
        所有 active 服务——包括 gpu_role=none 服务（llama_cpp/asr）。
        随后 GpuStateMachine.force_reset 降级为 kill+clear-only，
        force_kill_all 作为 SIGKILL 兜底安全网。
        """
        self._lifecycle._stop_all_active(include_none=True)
        return self._gpu_state.force_reset()

    # ── Delegation: ModelLifecycle ────────────────────────────────

    def stop_service(self, name: str) -> dict:
        return self._lifecycle.stop_service(name)

    def stop_independent(self, name: str) -> dict:
        return self._lifecycle.stop_independent(name)

    def list_independent(self) -> list[str]:
        return self._lifecycle.list_independent()

    def sleep_model(self, name: str) -> dict:
        return self._lifecycle.sleep_model(name)

    def wake_model(self, name: str) -> dict:
        return self._lifecycle.wake_model(name)

    # ── Discovery ─────────────────────────────────────────────────

    def discover_local_models(self) -> dict:
        """Scan ~/models/, ~/ComfyUI/models/, and Ollama for unconfigured models.

        Delegates to model_discovery.discover_local_models().
        """
        return _discover_local_models(self.models_dir)

    def auto_deploy(self, name: str, model_type: str) -> dict:
        """Auto-generate YAML and deploy a discovered model.

        Delegates YAML generation to model_discovery.auto_deploy(), then
        reloads self._models so the newly generated config is visible to
        the subsequent switch() call.
        """
        yaml_path = self.models_dir / f"{name}.yaml"
        if yaml_path.exists():
            return {"status": "already_configured",
                    "message": f"YAML already exists for {name}"}

        result = _auto_deploy(name, model_type, self.models_dir,
                              self._models, self.switch)
        # _auto_deploy writes YAML + calls switch_fn internally, but
        # switch_fn uses the stale self._models snapshot → first-time
        # deploy always fails with "Unknown model".  Reload and retry.
        self.reload_models()
        return self.switch(name)

    def pull_model(self, name: str, framework: str) -> dict:
        """Pull/download a model before deployment."""
        import subprocess, re
        # Validate name format to prevent abuse (allow ollama tags like qwen2.5:7b, library/model:tag)
        # Disallow path traversal (..) even though subprocess list form prevents shell injection
        if not re.match(r'^(?!.*\.\.)[\w.\-:/]+$', name):
            return {"status": "error", "message": f"Invalid model name: {name!r}"}
        if framework == "ollama":
            try:
                result = subprocess.run(["ollama", "pull", name], capture_output=True, text=True, timeout=1800)
                if result.returncode == 0:
                    return {"status": "pulled", "message": f"ollama model {name} pulled"}
                return {"status": "error", "message": result.stderr[:200]}
            except FileNotFoundError:
                return {"status": "error", "message": "ollama command not found on PATH"}
            except subprocess.TimeoutExpired:
                return {"status": "error", "message": "ollama pull timed out after 1800s"}
        elif framework in ("vllm", "llama_cpp"):
            return {"status": "error", "message": f"{framework} pull not supported yet — download manually"}
        else:
            return {"status": "error", "message": f"Unknown framework: {framework}"}


# ─── Backward Compatibility ──────────────────────────────────────

class ProfileManager(ModelManager):
    """Backward-compatible alias. All v3.x code using ProfileManager will work."""
    pass