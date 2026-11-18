"""
inferfabric/process_manager/sglang.py — SGLang process lifecycle sub-manager.
"""

import os
import time
import logging
import subprocess
from typing import Optional

from inferfabric.config import HEALTH_CHECK_TIMEOUT
from inferfabric.health import check_http_status, wait_gpu_free
from inferfabric.image_manager import ensure_image_pulled
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class SGLangProcessManager(BaseProcessManager):
    """SGLang Docker container lifecycle: start, stop."""

    # ─── PID accessors ──────────────────────────────────────────

    @property
    def sglang_pid(self):
        pid_str = self._state.get("sglang_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_sglang_pid(self, pid):
        self._state.set("sglang_pid", str(pid) if pid else "")

    @property
    def sglang_container(self):
        c = self._state.get("sglang_container")
        return c or None

    def _set_sglang_container(self, name):
        self._state.set("sglang_container", name or "")


    def start_sglang(self, cfg, container_name: str) -> dict:
        """Start SGLang via Docker container.

        container_name threaded from ModelConfig.container_name (single source).
        """
        log_file = self._log_dir / f"sglang_{cfg.served_name}.log"

        cmd = cfg.build_docker_cmd(container_name)
        env = os.environ.copy()

        # issue #3 L3b: 显式拉取 + 管理台账（拉取失败仅告警，docker run 自会显性失败）
        ensure_image_pulled(cfg.docker_image)

        log.info("Starting SGLang container %s: %s", container_name, " ".join(cmd))
        with open(log_file, "w") as f:
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        pgid = os.getpgid(proc.pid)
        self._set_sglang_pid(pgid)
        self._set_sglang_container(container_name)
        log.info("SGLang docker PID=%s PGID=%s container=%s", proc.pid, pgid, container_name)

        health_timeout = cfg.startup_timeout or HEALTH_CHECK_TIMEOUT
        start = time.time()
        while time.time() - start < health_timeout:
            time.sleep(2)
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                self._set_sglang_pid(None)
                self._set_sglang_container(None)
                return {"status": "error", "message": "SGLang container exited", "log": str(log_file)}
            if check_http_status(f"http://localhost:{cfg.port}/health", timeout=2) == "✅":
                return {"status": "healthy", "message": f"SGLang healthy on port {cfg.port}"}
        self.stop_sglang(port=cfg.port, container_name=container_name)
        return {"status": "timeout", "message": f"SGLang didn't become healthy within {health_timeout}s"}

    def stop_sglang(self, port: Optional[int] = None, container_name: Optional[str] = None) -> dict:
        """Stop SGLang container via `docker stop` (graceful SIGTERM→SIGKILL).

        D3: unified to graceful docker stop (was docker kill). SGLang's
        `docker run --rm` auto-removes on stop, so no separate docker rm.
        Falls back to scanning running containers if no name is tracked.
        """
        if not container_name:
            container_name = self.sglang_container
        if not container_name:
            # Fallback: scan for sglang-* containers and stop any on this port
            try:
                r = subprocess.run(
                    ["docker", "ps", "--filter", "name=sglang-", "--format", "{{.Names}}:{{.Ports}}"],
                    timeout=10, capture_output=True, text=True, check=False
                )
                for line in r.stdout.strip().split("\n"):
                    if not line:
                        continue
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        cname, cports = parts
                        if str(port) in cports:
                            container_name = cname
                            log.info("Discovered SGLang container %s (port %d)", container_name, port)
                            break
            except Exception as e:
                log.debug("Docker scan fallback failed: %s", e)
        if container_name:
            try:
                result = subprocess.run(["docker", "stop", container_name],
                              timeout=10, check=False, capture_output=True)
            except subprocess.TimeoutExpired:
                msg = f"docker stop {container_name} timed out"
                log.warning(msg)
                return {"status": "warning", "message": msg}
            except FileNotFoundError:
                msg = "docker not found on PATH"
                log.warning(msg)
                return {"status": "warning", "message": msg}
            if result.returncode != 0:
                stderr_fragment = result.stderr.decode()[:200] if result.stderr else ""
                msg = f"docker stop exit {result.returncode}: {stderr_fragment}"
                log.warning(msg)
                self._set_sglang_pid(None)
                self._set_sglang_container(None)
                return {"status": "warning", "message": msg}
            log.info("SGLang container %s stopped (graceful)", container_name)

        if port:
            try:
                # DeviceTelemetry T3: fail-closed — wait_gpu_free returns
                # False on an unavailable reading, never reports free.
                wait_gpu_free(telemetry=self.device_telemetry)
            except Exception as e:
                log.error("GPU did not free after SGLang stop (port %s): %s", port, e)
        self._set_sglang_pid(None)
        self._set_sglang_container(None)
        return {"status": "ok", "message": "SGLang container stopped"}

    def is_sglang_alive(self, port: int) -> bool:
        return check_http_status(f"http://localhost:{port}/health", timeout=2) == "✅"

    # ─── SGLang ────────────────────────────────────────────────────

