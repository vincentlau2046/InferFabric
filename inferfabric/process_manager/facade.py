"""
inferfabric/process_manager/facade.py — ProcessManager facade.

Delegates per-engine lifecycle methods to sub-managers.
PID properties read directly from StateDB (shared with sub-managers).
stop_all / force_kill_all orchestrate across all engines.
"""

import os
import re as _re
import time
import signal
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.config import DEFAULT_LOG_DIR, COMFYUI_DIR, GPU_AUTO_CLEAR_CUDA_STATE
from inferfabric.health import check_http_status
from inferfabric.process_manager.base import BaseProcessManager
from inferfabric.process_manager.vllm import VLLMProcessManager
from inferfabric.process_manager.sglang import SGLangProcessManager
from inferfabric.process_manager.comfyui import ComfyUIProcessManager
from inferfabric.process_manager.tts import TTSProcessManager
from inferfabric.process_manager.asr import ASRProcessManager
from inferfabric.process_manager.ollama_cpp import OllamaCppProcessManager
from inferfabric.process_manager.ninfer import NInferProcessManager

log = logging.getLogger("inferfabric")


class ProcessManager(BaseProcessManager):
    """Unified process lifecycle facade for all engine types.

    API-compatible with the original monolithic ProcessManager.
    Delegates per-engine operations to dedicated sub-managers.
    """

    def __init__(self, state, log_dir: Path = DEFAULT_LOG_DIR):
        super().__init__(state, log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._vllm = VLLMProcessManager(state, log_dir)
        self._sglang = SGLangProcessManager(state, log_dir)
        self._comfyui = ComfyUIProcessManager(state, log_dir)
        self._tts = TTSProcessManager(state, log_dir)
        self._asr = ASRProcessManager(state, log_dir)
        self._ollama = OllamaCppProcessManager(state, log_dir)
        self._ninfer = NInferProcessManager(state, log_dir)

    # ─── PID Tracking ────────────────────────────────────────────

    @property
    def vllm_pid(self) -> Optional[int]:
        pid_str = self._state.get("vllm_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def comfyui_pid(self) -> Optional[int]:
        pid_str = self._state.get("comfyui_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def sglang_pid(self) -> Optional[int]:
        pid_str = self._state.get("sglang_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def sglang_container(self) -> Optional[str]:
        c = self._state.get("sglang_container")
        return c or None

    @property
    def ninfer_pid(self) -> Optional[int]:
        pid_str = self._state.get("ninfer_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def ninfer_container(self) -> Optional[str]:
        c = self._state.get("ninfer_container")
        return c or None

    @property
    def tts_pid(self) -> Optional[int]:
        pid_str = self._state.get("tts_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def asr_pid(self) -> Optional[int]:
        pid_str = self._state.get("asr_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    # ─── PID Setters (test compat, delegate to sub-managers via state) ──

    def _set_vllm_pid(self, pid: Optional[int]):
        self._state.set("vllm_pid", str(pid) if pid else "")

    def _set_comfyui_pid(self, pid: Optional[int]):
        self._state.set("comfyui_pid", str(pid) if pid else "")

    def _set_sglang_pid(self, pid: Optional[int]):
        self._state.set("sglang_pid", str(pid) if pid else "")

    def _set_sglang_container(self, name: Optional[str]):
        self._state.set("sglang_container", name or "")

    def _set_ninfer_pid(self, pid: Optional[int]):
        self._state.set("ninfer_pid", str(pid) if pid else "")

    def _set_ninfer_container(self, name: Optional[str]):
        self._state.set("ninfer_container", name or "")

    def _set_tts_pid(self, pid: Optional[int]):
        self._state.set("tts_pid", str(pid) if pid else "")

    def _set_asr_pid(self, pid: Optional[int]):
        self._state.set("asr_pid", str(pid) if pid else "")

    # ─── vLLM ────────────────────────────────────────────────────

    def start_vllm(self, cfg) -> dict:
        return self._vllm.start_vllm(cfg)

    def stop_vllm(self, port: Optional[int] = None) -> dict:
        return self._vllm.stop_vllm(port)

    def sleep_vllm(self, port: int) -> dict:
        return self._vllm.sleep_vllm(port)

    def wake_vllm(self, port: int) -> dict:
        return self._vllm.wake_vllm(port)

    def is_sleeping(self, port: int) -> bool:
        return self._vllm.is_sleeping(port)

    def is_vllm_alive(self, port: int) -> bool:
        return self._vllm.is_vllm_alive(port)

    # ─── SGLang ─────────────────────────────────────────────────

    def start_sglang(self, cfg, container_name: str) -> dict:
        return self._sglang.start_sglang(cfg, container_name)

    def stop_sglang(self, port: Optional[int] = None, container_name: Optional[str] = None) -> dict:
        return self._sglang.stop_sglang(port=port, container_name=container_name)

    def start_ninfer(self, cfg, container_name: str) -> dict:
        return self._ninfer.start_ninfer(cfg, container_name)

    def stop_ninfer(self, container_name: str) -> dict:
        return self._ninfer.stop_ninfer(container_name)

    def engine_exists(self, model) -> bool:
        """PR-20 ②（评审修复 3）：现实识别 —— 本模型自己的引擎是否已在跑。

        非变更性身份探测（容器名 + 镜像匹配），供 lifecycle 在新分配守卫
        （CUDA clear + VRAM 预算）之前判定「分配目标已被本模型引擎占用」。
        必须返回字面 True / False（消费方用 `is True` 判定，防 MagicMock
        truthy 误跳守卫）。当前仅 NInfer 容器支持（proxy 重启幸存场景）；
        其余引擎 → False（照走新分配守卫路径）。
        """
        if getattr(model, "type", None) == "ninfer" and model.ninfer:
            return self._ninfer.container_exists(model.ninfer, model.container_name)
        return False

    def is_sglang_alive(self, port: int) -> bool:
        return self._sglang.is_sglang_alive(port)

    # ─── ComfyUI ─────────────────────────────────────────────────

    def start_comfyui(self, cfg) -> dict:
        return self._comfyui.start_comfyui(cfg)

    def stop_comfyui(self, port: Optional[int] = None) -> dict:
        return self._comfyui.stop_comfyui(port)

    def stop_comfyui_with_config(self, cfg, port: Optional[int] = None) -> dict:
        return self._comfyui.stop_comfyui_with_config(cfg, port)

    def is_comfyui_alive(self, port: int = 8188) -> bool:
        return self._comfyui.is_comfyui_alive(port)

    # ─── Ollama.cpp ──────────────────────────────────────────────

    def start_ollama_cpp(self, cfg, model_type: str = "") -> dict:
        return self._ollama.start_ollama_cpp(cfg, model_type)

    def stop_ollama_cpp(self, port: Optional[int] = None):
        self._ollama.stop_ollama_cpp(port)

    def run_ollama(self, model_ref: str, keep_alive: str = "5m", num_gpu: int = -1) -> dict:
        return self._ollama.run_ollama(model_ref, keep_alive, num_gpu)

    def start_ollama(self, model) -> dict:
        """Start an Ollama model — ensure daemon, then pull/load the model.

        Moved from ModelLifecycle._start_ollama_model so the EngineAdapter can
        follow the same delegation pattern as every other adapter type.
        """
        daemon_healthy = check_http_status("http://localhost:11434/api/tags")
        if daemon_healthy != "\u2705":
            log.info("Ollama daemon not running — auto-starting")
            try:
                env = os.environ.copy()
                model_ref = model.ollama.model_ref
                num_gpu = getattr(model.ollama, "num_gpu", -1)
                if num_gpu >= 0:
                    env["OLLAMA_NUM_GPU"] = str(num_gpu)
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            except FileNotFoundError:
                return {"status": "error", "message": "ollama not found in PATH. Install Ollama first."}
            # Wait for daemon to become healthy (up to 30 s)
            for _ in range(60):
                time.sleep(0.5)
                if check_http_status("http://localhost:11434/api/tags") == "\u2705":
                    break
            else:
                return {"status": "error", "message": "Ollama daemon failed to start within 30s"}

        model_ref = model.ollama.model_ref
        keep_alive = model.ollama.keep_alive or "5m"
        num_gpu = getattr(model.ollama, "num_gpu", -1)
        return self.run_ollama(model_ref, keep_alive, num_gpu)

    # ─── TTS Server ─────────────────────────────────────────────

    def start_tts_server(self, cfg) -> dict:
        return self._tts.start_tts_server(cfg)

    def stop_tts_server(self, port: Optional[int] = None) -> dict:
        return self._tts.stop_tts_server(port)

    # ─── ASR Server ─────────────────────────────────────────────

    def start_asr_server(self, cfg) -> dict:
        return self._asr.start_asr_server(cfg)

    def stop_asr_server(self, port: Optional[int] = None) -> dict:
        return self._asr.stop_asr_server(port)

    # ─── Combined Operations ─────────────────────────────────────

    def stop_all(
        self,
        comfyui_cfg=None,
        vllm_ports: Optional[list[int]] = None,
        comfyui_port: Optional[int] = None,
        tts_port: Optional[int] = None,
        asr_port: Optional[int] = None,
        sglang_ports: Optional[list[int]] = None,
        active_services: Optional[list[str]] = None,
    ) -> dict:
        """Stop services: ComfyUI first, then vLLM, then TTS, then ASR,
        then SGLang.

        Two contracts:

        - **active_services given (not None)**: the caller has already
          stopped the enumerated services via the engine adapter
          (``get_adapter(model.type).stop(model)``), so no bare
          ``stop_<engine>()`` call is made — this method only clears the
          tracked PIDs so the facade stays self-contained.  Explicit
          port/cfg args (``vllm_ports``, ``comfyui_cfg``, ``tts_port``,
          ``asr_port``, ``sglang_ports``) still trigger their engine stops.

        - **active_services is None (legacy)**: stop everything — bare
          ``stop_<engine>`` calls for vLLM/ComfyUI/TTS/ASR, which clear
          their own PIDs.  Backward compatible for callers that do not
          track which services are running.

        SGLang always requires explicit ports (no bare stop path);
        force_kill_all is unchanged.
        """
        results = {}

        # ComfyUI
        if comfyui_cfg:
            port = comfyui_port or comfyui_cfg.port
            results["comfyui"] = self.stop_comfyui_with_config(comfyui_cfg, port=port)
        elif active_services is None:
            results["comfyui"] = self.stop_comfyui()

        # vLLM
        if vllm_ports:
            for p in vllm_ports:
                self.stop_vllm(port=p)
            results["vllm"] = {"status": "ok", "ports": vllm_ports}
        elif active_services is None:
            results["vllm"] = self.stop_vllm()

        # TTS
        if tts_port:
            results["tts"] = self.stop_tts_server(port=tts_port)
        elif active_services is None:
            results["tts"] = self.stop_tts_server()

        # ASR
        if asr_port:
            results["asr"] = self.stop_asr_server(port=asr_port)
        elif active_services is None:
            results["asr"] = self.stop_asr_server()

        # SGLang (already guarded by explicit ports — keep existing logic)
        if sglang_ports:
            for p in sglang_ports:
                self.stop_sglang(port=p)
            results["sglang"] = {"status": "ok", "ports": sglang_ports}

        # active_services given: engines were stopped by the caller via the
        # adapter path — clear tracked PIDs so the facade is self-contained.
        if active_services is not None:
            self._set_vllm_pid(None)
            self._set_sglang_pid(None)
            self._set_comfyui_pid(None)
            self._set_tts_pid(None)
            self._set_asr_pid(None)
            self._set_sglang_container(None)
            self._set_ninfer_pid(None)
            self._set_ninfer_container(None)

        return results

    # ─── GPU CUDA State Reset ──────────────────────────────

    def clear_gpu_cuda_state(self, gpu_index: int = 0, force: bool = False) -> dict:
        """Public: clear GPU CUDA driver state to eliminate fragmentation.

        Delegates to the base class method. Call this after stopping all
        GPU-bound services and before starting an exclusive model, to
        ensure the CUDA driver’s internal memory map is clean.

        When ``GPU_AUTO_CLEAR_CUDA_STATE`` is False, this is a no-op
        unless ``force=True`` (used by CLI 'gpu-clear' command).

        Returns dict with status and method used.
        """
        if not GPU_AUTO_CLEAR_CUDA_STATE and not force:
            log.debug("GPU CUDA state clear disabled by GPU_AUTO_CLEAR_CUDA_STATE=False")
            return {"status": "skipped", "message": "disabled by config"}
        return self._clear_gpu_cuda_state(gpu_index)

    def force_kill_all(self) -> dict:
        """Nuclear option: SIGKILL everything related to vLLM + ComfyUI + TTS + ASR + SGLang."""
        # vLLM
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "sglang serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "sglang.launch_server"], timeout=5, check=False)
        for port in [8000, 8001, 8002]:
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)

        # ComfyUI
        cpgid = self.comfyui_pid
        if cpgid:
            try:
                os.killpg(cpgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", f"python.*{_re.escape(str(COMFYUI_DIR))}/main.py"], timeout=5, check=False)
        comfyui_dir = _re.escape(str(COMFYUI_DIR))
        subprocess.run(["pkill", "-9", "-f", f"python.*{comfyui_dir}"], timeout=5, check=False)

        # TTS
        tpgid = self.tts_pid
        if tpgid:
            try:
                os.killpg(tpgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "Qwen3-TTS-Openai-Fastapi/api"], timeout=5, check=False)
        subprocess.run(["fuser", "-k", "8880/tcp"], timeout=5, check=False)

        # ASR
        apgid = self.asr_pid
        if apgid:
            try:
                os.killpg(apgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "funasr-server"], timeout=5, check=False)
        subprocess.run(["fuser", "-k", "8881/tcp"], timeout=5, check=False)

        time.sleep(2)
        if GPU_AUTO_CLEAR_CUDA_STATE:
            self._clear_gpu_cuda_state()  # clear fragmentation after force kill
        self._state.set("vllm_pid", "")
        self._state.set("comfyui_pid", "")
        self._state.set("tts_pid", "")
        self._state.set("asr_pid", "")
        self._cleanup_pid_files("vllm")
        self._cleanup_pid_files("comfyui")
        self._cleanup_pid_files("tts")
        self._cleanup_pid_files("asr")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok"}