"""
inferfabric/process_manager/vllm.py — vLLM process lifecycle sub-manager.
"""

import os
import time
import signal
import json
import logging
import urllib.request
import urllib.error
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.config import (
    CONDA_ENVS,
    VLLM_STARTUP_CHECK_INTERVAL,
    VLLM_STARTUP_CHECK_ROUNDS,
    HEALTH_CHECK_TIMEOUT,
    STOP_SIGTERM_TIMEOUT,
    VLLMConfig,
    ConfigError,
)
from inferfabric.health import wait_http, check_http_status
from inferfabric.image_manager import ensure_image_pulled
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class VLLMProcessManager(BaseProcessManager):
    """vLLM process lifecycle: start, stop, sleep, wake."""

    # ─── PID accessors ──────────────────────────────────────────

    @property
    def vllm_pid(self):
        pid_str = self._state.get("vllm_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_vllm_pid(self, pid):
        self._state.set("vllm_pid", str(pid) if pid else "")


    def start_vllm(self, cfg: VLLMConfig) -> dict:
        """Start vLLM via conda env's vllm binary. Uses start_new_session for process group isolation.

        Model-specific environment variables are injected from cfg.extra_env (YAML-defined).
        """
        log_file = self._log_dir / f"vllm_{cfg.conda_env}.log"
        pid_file = self._log_dir / f"vllm_{cfg.conda_env}.pid"

        vllm_bin = CONDA_ENVS / cfg.conda_env / "bin" / "vllm"
        if not vllm_bin.exists():
            log.error("vllm binary not found: %s", vllm_bin)
            return {"status": "error", "message": f"vllm not found in conda env {cfg.conda_env}"}

        cmd = cfg.build_cmd()
        cmd[0] = str(vllm_bin)

        log.info("Starting vLLM cmd: %s", " ".join(cmd[:8]) + "...")
        env = dict(os.environ)
        # KV offloading conflicts with expandable_segments (NIXL/Mooncake IB memory)
        has_kv_offload = "--kv-offloading-size" in " ".join(cmd)
        if not has_kv_offload:
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        else:
            log.info("KV offloading detected — skipping expandable_segments for %s", cfg.served_name)

        # Enable sleep mode if configured
        if cfg.sleep_mode and cfg.sleep_mode.enabled:
            env["VLLM_SERVER_DEV_MODE"] = "1"
            cmd.append("--enable-sleep-mode")
            log.info("Sleep mode enabled (L2) for %s", cfg.served_name)

        # Inject extra_env from model YAML config (highest priority)
        if cfg.extra_env:
            if cfg.sleep_mode and cfg.sleep_mode.enabled and "VLLM_SERVER_DEV_MODE" in cfg.extra_env:
                raise ConfigError(
                    f"extra_env overrides VLLM_SERVER_DEV_MODE for {cfg.served_name} — "
                    f"sleep mode will not work. Remove VLLM_SERVER_DEV_MODE from extra_env or disable sleep_mode."
                )
            for k, v in cfg.extra_env.items():
                env[k] = v
                log.debug("extra_env: %s=%s", k, v)

        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file.write_text("")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start vLLM: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # With start_new_session, PID == PGID
        self._set_vllm_pid(pgid)
        pid_file.write_text(str(pgid))
        log.info("vLLM started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Check if process died immediately
        for _ in range(VLLM_STARTUP_CHECK_ROUNDS):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("vLLM exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_vllm_pid(None)
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"vLLM exited with code {ret}", "log": str(log_file)}

            time.sleep(VLLM_STARTUP_CHECK_INTERVAL)

        # Wait for vLLM to become healthy (use model-specific timeout if configured)
        health_timeout = cfg.startup_timeout if cfg.startup_timeout > 0 else HEALTH_CHECK_TIMEOUT
        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=health_timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                return {"status": "error", "message": "vLLM crashed during loading", "log": str(log_file)}
            else:
                self.stop_vllm()
                return {"status": "timeout", "message": f"vLLM didn't become healthy within {health_timeout}s"}

    def start_vllm_docker(self, cfg: VLLMConfig, container_name: str) -> dict:
        """Start vLLM via Docker container (deployment: docker).

        Mirrors start_sglang / start_ninfer. container_name from
        ModelConfig.container_name (single source). start_new_session →
        PID == PGID, so the tracked vllm_pid is a valid os.killpg target
        for gpu_state liveness checks (the `docker run` client stays alive
        while the container runs, exits when it stops).
        """
        log_file = self._log_dir / f"vllm_{cfg.served_name}.log"

        cmd = cfg.build_docker_cmd(container_name)
        env = os.environ.copy()

        # issue #3 L3b: 显式拉取 + 管理台账；issue #8-③（选项 2′）：
        # 拉取失败即中止启动——不再让 docker run 无超时地隐式重拉
        # （30GB 级镜像的真实 stall 场景），错误信息含原因与重试指引。
        pull = ensure_image_pulled(cfg.docker_image)
        if pull.get("status") == "error":
            return {"status": "error",
                    "message": (f"image pull failed for {cfg.docker_image}: "
                                f"{pull.get('detail') or 'docker pull failed'} — "
                                f"retry the start or run: docker pull {cfg.docker_image}")}

        log.info("Starting vLLM container %s: %s", container_name, " ".join(cmd[:8]) + "...")
        with open(log_file, "w") as f:
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        pgid = proc.pid  # With start_new_session, PID == PGID
        self._set_vllm_pid(pgid)
        log.info("vLLM docker PID=%s PGID=%s container=%s", proc.pid, pgid, container_name)

        health_timeout = cfg.startup_timeout if cfg.startup_timeout > 0 else HEALTH_CHECK_TIMEOUT
        start = time.time()
        while time.time() - start < health_timeout:
            time.sleep(2)
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                log.error("vLLM container exited (ret=%s): %s", proc.poll(), err[-500:])
                self._set_vllm_pid(None)
                return {"status": "error", "message": "vLLM container exited",
                        "log": str(log_file)}
            if check_http_status(f"http://localhost:{cfg.port}/health", timeout=2) == "✅":
                return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        # Health timeout → best-effort docker stop cleanup
        subprocess.run(["docker", "stop", container_name], timeout=10,
                       capture_output=True, check=False)
        self._set_vllm_pid(None)
        return {"status": "timeout",
                "message": f"vLLM didn't become healthy within {health_timeout}s"}

    def stop_vllm(self, port: Optional[int] = None) -> dict:
        """Stop vLLM using process group kill. SIGTERM → wait → SIGKILL entire group.

        When ``port`` is supplied, also does port-based cleanup after the tracked
        PID path completes (or immediately if the tracked PID is dead).  This
        provides defence-in-depth: if the tracked PID drifts (e.g. stopped via
        external signal), port matching catches the leftover process.
        """
        pgid = self.vllm_pid
        if pgid is None and port is None:
            log.warning("No vLLM PID tracked and no port given — falling back to pkill")
            return self._pkill_vllm_fallback()

        # P1-2: 校验 PID 是否已被操作系统复用到无关进程
        if pgid is not None and not self._validate_pid(pgid, "vllm"):
            log.warning(
                "Tracked vLLM PID %d does not appear to be a vLLM process "
                "(cmdline mismatch) — clearing stale PID and using fallback",
                pgid,
            )
            self._set_vllm_pid(None)
            if port:
                self._pkill_by_port(port)
                self._cleanup_pid_files("vllm")
                self._wait_gpu_idle()
                return {"status": "ok", "message": "port-based cleanup after stale PID"}
            return self._pkill_vllm_fallback()

        if pgid is None:
            # Tracked PID gone but port given — skip PG path, go straight to port cleanup
            log.info("Tracked PID gone but port=%d given — port-based cleanup only", port)
            self._pkill_by_port(port)
            self._set_vllm_pid(None)
            self._cleanup_pid_files("vllm")
            self._wait_gpu_idle()
            return {"status": "ok", "message": "port-based cleanup"}

        log.info("Stopping vLLM PGID=%d", pgid)

        # SIGTERM the process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("Process group %d already dead", pgid)
            self._set_vllm_pid(None)
            self._cleanup_pid_files("vllm")
            if port:
                self._pkill_by_port(port)
            return {"status": "ok", "message": "already dead"}

        # Wait for graceful shutdown
        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                log.info("vLLM process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_vllm_pid(None)
                self._cleanup_pid_files("vllm")
                self._reap_zombies()
                self._wait_gpu_idle()
                if port:
                    self._pkill_by_port(port)
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        # SIGKILL
        log.warning("SIGTERM timeout for PGID %d, sending SIGKILL to group", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_vllm_pid(None)
        self._cleanup_pid_files("vllm")
        self._reap_zombies()
        if port:
            self._pkill_by_port(port)
        self._wait_gpu_idle()
        return {"status": "ok", "message": "killed (SIGKILL)"}

    def _pkill_vllm_fallback(self) -> dict:
        """Fallback: stop vLLM when no PID is tracked.

        Strategy (safe, no global pkill -f):
          1. Scan PID files in log_dir → kill -9 <pid> → wait.
          2. For each known vLLM port: fuser <port>/tcp (only kills on that port).
        """
        # Step 1: Kill by PID files
        for pf in sorted(self._log_dir.glob("vllm_*.pid")):
            try:
                pid = int(pf.read_text().strip())
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                        log.info("%s vLLM PID %d via PID file %s",
                                 "SIGTERM" if sig == signal.SIGTERM else "SIGKILL",
                                 pid, pf.name)
                    except (ProcessLookupError, PermissionError):
                        pass
                time.sleep(1)
            except (ValueError, OSError) as e:
                log.debug("Failed to read PID from %s: %s", pf, e)
            pf.unlink(missing_ok=True)

        # Step 2: Port-based cleanup via fuser (last resort)
        vllm_ports = []
        try:
            from inferfabric.config import load_models, MODELS_DIR
            for m in load_models(MODELS_DIR).values():
                if m.vllm:
                    vllm_ports.append(m.vllm.port)
        except Exception as e:
            log.warning("load_models failed, falling back to hardcoded ports: %s", e)
            vllm_ports = [8000, 8001, 8002]

        for port in vllm_ports:
            self._pkill_by_port(port)

        time.sleep(2)
        self._cleanup_pid_files("vllm")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok", "message": "pkill fallback"}

    # ─── vLLM ─────────────────────────────────────────────────

    def sleep_vllm(self, port: int) -> dict:
        """Put vLLM server to L2 sleep (discard weights, free VRAM)."""
        url = f"http://localhost:{port}/sleep?level=2"
        log.info("Sleeping vLLM at port %d (L2)", port)
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed = round(time.time() - t0, 1)
                log.info("vLLM sleep OK (port=%d, %.1fs)", port, elapsed)
                return {"status": "ok", "port": port, "elapsed_sec": elapsed}
        except urllib.error.HTTPError as e:
            elapsed = round(time.time() - t0, 1)
            log.error("vLLM sleep HTTP %d (port=%d): %s", e.code, port, e.reason)
            return {"status": "error", "message": f"HTTP {e.code}: {e.reason}", "elapsed_sec": elapsed}
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            log.error("vLLM sleep failed (port=%d): %s", port, e)
            return {"status": "error", "message": str(e), "elapsed_sec": elapsed}

    def wake_vllm(self, port: int) -> dict:
        """L2 wake: kill sleeping process, then cold restart via switch.

        vLLM 0.23.0 L2 sleep leaves the engine in an unrecoverable state
        (wake_up CUDA invalid argument). We kill the sleeping process
        and let the caller handle restart.
        """
        log.info("Killing sleeping vLLM at port %d for restart", port)
        self.stop_vllm()
        return {"status": "killed_for_restart", "port": port, "elapsed_sec": 0}

    def is_sleeping(self, port: int) -> bool:
        """Check if vLLM server is currently in sleep mode."""
        try:
            req = urllib.request.Request(f"http://localhost:{port}/is_sleeping")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("is_sleeping", False)
        except Exception:
            return False

    def is_vllm_alive(self, port: int) -> bool:
        """Check if vLLM process is still alive (by PID or HTTP)."""
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        return check_http_status(f"http://localhost:{port}/health") != "❌"

