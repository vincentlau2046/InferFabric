"""
inferfabric/process_manager/ninfer.py — NInfer Docker container lifecycle.

Mirrors VLLMProcessManager / SGLangProcessManager structure (D1).
start_ninfer / stop_ninfer migrated from the inline docker run/stop in
engine_adapter/ninfer.py — container_name now a parameter (single source
of truth = ModelConfig.container_name, threaded by the adapter).
"""

import os
import time
import logging
import subprocess
from pathlib import Path

from inferfabric.health import wait_http, check_http_status
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


def _parse_docker_ts(ts: str) -> float | None:
    """docker inspect StartedAt（RFC3339Nano）→ epoch 秒；不可解析返回 None。

    docker 输出纳秒小数（最多 9 位），py3.10 fromisoformat 只接受 ≤6 位 →
    截断到微秒；'Z' 后缀 3.11+ 才识别 → 统一转 +00:00。
    """
    if not ts:
        return None
    import re
    ts = re.sub(r"\.(\d{6})\d+", r".\1", ts)
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


class NInferProcessManager(BaseProcessManager):
    """NInfer Docker container lifecycle: start, stop. Mirrors SGLangProcessManager."""

    # ─── PID / container accessors ──────────────────────────────

    @property
    def ninfer_pid(self):
        pid_str = self._state.get("ninfer_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_ninfer_pid(self, pid):
        self._state.set("ninfer_pid", str(pid) if pid else "")

    @property
    def ninfer_container(self):
        c = self._state.get("ninfer_container")
        return c or None

    def _set_ninfer_container(self, name):
        self._state.set("ninfer_container", name or "")

    # ─── start ──────────────────────────────────────────────────

    def container_exists(self, cfg, container_name: str) -> bool:
        """评审修复 3：非变更性身份探测 —— lifecycle 跳过新分配守卫的依据。

        只回答「这个模型自己的容器是否在跑且镜像匹配」。健康/年龄的三态
        决策仍归 start_ninfer（在场 ⇒ 收养或等待，都不分配新 VRAM；重建
        路径先 docker stop 释放旧占用再分配）。因此加载中的容器也算在场
        —— 正是 P0 的 15min 冷加载场景，不能被 VRAM 预算守卫自拒。
        绝不 docker stop/run、不写 state。
        """
        info = self._container_info(container_name)
        if info is None:
            return False
        # 身份验证缺一不可：镜像引用为空 → 无法证明容器是本模型的 → 不认领
        if not (info.get("image") and cfg.docker_image):
            return False
        return info["image"] == cfg.docker_image

    def _container_info(self, container_name: str) -> dict | None:
        """docker inspect 一次取收养判定所需信息（PR-20 ②）。

        Returns: None = 容器不存在 / 未运行 / docker 不可用 / 输出不可解析
        （统一按"无容器"处理，走原创建路径）；否则
        {"running": True, "started_at": epoch秒|None, "image": str|None}。
        """
        try:
            r = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{.State.Running}}\t{{.State.StartedAt}}\t{{.Config.Image}}",
                 container_name],
                timeout=10, capture_output=True, text=True, check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if r.returncode != 0:
            return None
        stdout = r.stdout
        if not isinstance(stdout, str):
            return None  # 非文本输出（测试替身等）→ 按无容器处理
        parts = stdout.strip().split("\t")
        if len(parts) < 3 or parts[0] != "true":
            return None  # 存在但未运行（--rm 移除竞态窗口）→ 重建路径兜底
        return {"running": True,
                "started_at": _parse_docker_ts(parts[1]),
                "image": parts[2] or None}

    def _try_adopt(self, cfg, container_name: str, info: dict) -> dict | None:
        """PR-20 ② 三态收养决策。返回收养结果 dict；None = 走重建路径。

        P0 实测：引擎容器天然不随 proxy 进程死亡（bge-m3 幸存 + alpine
        信号实验），旧的无条件 pre-start docker stop 才是重启后 ~15min
        冷启的元凶。三态：
          健在且健康   → 收养（零冷启，不动容器、不 sleep）
          年轻未就绪   → 等宽限剩余（IFF_NINFER_ADOPT_GRACE，默认 1200s，
                         覆盖实测 15min NVFP4 冷加载；不用 startup_timeout
                         ——它只覆盖全新启动的健康等待，挡不住加载中场景）
          年老不健康 /
          镜像变更     → 重建（None）
        """
        url = f"http://localhost:{cfg.port}/v1/models"
        age = (time.time() - info["started_at"]) if info["started_at"] else None
        grace = float(os.environ.get("IFF_NINFER_ADOPT_GRACE", "1200"))
        if info["image"] and cfg.docker_image and info["image"] != cfg.docker_image:
            log.info("NInfer rebuild: container %s image %s != cfg %s — 镜像已变更，重建生效",
                     container_name, info["image"], cfg.docker_image)
            return None
        if check_http_status(url, timeout=3) == "✅":
            self._set_ninfer_pid(None)  # 收养容器：docker CLI 属已死旧进程，无本进程子 PID
            self._set_ninfer_container(container_name)
            log.info("NInfer adopt: container %s healthy on :%d — reusing (no cold start)",
                     container_name, cfg.port)
            return {"status": "healthy", "port": cfg.port, "pid": None, "adopted": True}
        if age is not None and age < grace:
            wait_s = max(int(grace - age), 1)
            log.info("NInfer adopt-wait: %s age %.0fs < grace %.0fs — waiting up to %ds（疑似加载中）",
                     container_name, age, grace, wait_s)
            if wait_http(url, timeout=wait_s):
                self._set_ninfer_pid(None)
                self._set_ninfer_container(container_name)
                log.info("NInfer adopt: %s became healthy — reusing", container_name)
                return {"status": "healthy", "port": cfg.port, "pid": None, "adopted": True}
            log.warning("NInfer adopt-wait expired for %s — rebuilding", container_name)
            return None
        log.warning("NInfer %s running but unhealthy (age=%s) — rebuilding",
                    container_name, "unknown" if age is None else f"{age:.0f}s")
        return None

    def start_ninfer(self, cfg, container_name: str) -> dict:
        """Start NInfer via Docker container — PR-20 ② 三态收养。

        Migrated from engine_adapter/ninfer.py:63-124. container_name is now
        a parameter (was cfg.container_name or f"ninfer-{cfg.port}").

        重启零中断：先探测既有容器，健在健康 → 直接收养（不 docker stop）；
        否则回落原 pre-start cleanup 路径（docker stop stale + 2s settle）。
        返回值兼容：收养返回 status="healthy" + adopted=True（消费方
        model_lifecycle 只认 healthy/started/ok，不感知收养）。
        """
        if not cfg:
            return {"status": "error", "message": "No ninfer config"}

        info = self._container_info(container_name)
        if info and info["running"]:
            adopted = self._try_adopt(cfg, container_name, info)
            if adopted is not None:
                return adopted

        # Pre-start cleanup: stop any stale container with the same name (避免重名冲突)
        subprocess.run(["docker", "stop", container_name], timeout=10,
                       capture_output=True, check=False)
        time.sleep(2)

        weight_path = Path(cfg.weight_path).expanduser()
        weight_dir = str(weight_path.parent)

        cmd = [
            "docker", "run", "--gpus", "all", "--rm",
            "-v", f"{weight_dir}:/workspace",
            "-p", f"{cfg.port}:8080",
            "--name", container_name,
            "-e", "NVIDIA_DISABLE_REQUIRE=1",
            cfg.docker_image,
            "ninfer-serve", weight_path.name,
            "--model-id", cfg.model_id,
            "--host", "0.0.0.0", "--port", "8080",
            "--max-concurrency", str(cfg.max_concurrency),
            "--max-context", str(cfg.max_context),
            "--kv-capacity", "auto" if cfg.kv_capacity == 0 else str(cfg.kv_capacity),
            "--default-max-tokens", str(cfg.default_max_tokens),
            "--pending-timeout-ms", str(cfg.pending_timeout_ms),
            "--kv-dtype", cfg.kv_dtype,
            "--prefill-chunk", str(cfg.prefill_chunk),
        ]
        if cfg.enable_mtp:
            cmd.extend(["--spec", "mtp", "--draft-tokens", str(cfg.draft_tokens)])
        if cfg.enable_lm_head_draft:
            cmd.append("--lm-head-draft")
        if getattr(cfg, "vision", False):
            cmd.append("--vision")
        # media 显存是可压缩空间: 压 media-cache/live 可把剩余显存让给 KV auto 编排
        mem = getattr(cfg, "media_cache_mib", 1024)
        if mem >= 0:
            cmd.extend(["--media-cache-mib", str(mem)])
        mem = getattr(cfg, "media_live_mib", 2048)
        if mem > 0:
            cmd.extend(["--media-live-mib", str(mem)])

        log.info("Starting NInfer: %s", " ".join(cmd))
        log_file = Path(cfg.log_file or f"/tmp/ninfer-{cfg.port}.log")
        log_file.write_text("")

        try:
            proc = subprocess.Popen(
                cmd, stdout=log_file.open("a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start NInfer: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_ninfer_pid(pgid)
        self._set_ninfer_container(container_name)
        log.info("NInfer docker PID=%s container=%s", proc.pid, container_name)

        # Immediate-exit detection (保留 ninfer adapter:117-120 的 6 轮 poll)
        for _ in range(6):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("NInfer exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_ninfer_pid(None)
                self._set_ninfer_container(None)
                return {"status": "error", "message": f"NInfer exited with code {ret}",
                        "log": str(log_file)}
            time.sleep(0.5)

        timeout = cfg.startup_timeout or 120
        healthy = wait_http(f"http://localhost:{cfg.port}/v1/models", timeout=timeout)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        # 健康超时 → 清理
        self.stop_ninfer(container_name)
        return {"status": "timeout",
                "message": f"NInfer didn't become healthy within {timeout}s"}

    # ─── stop ───────────────────────────────────────────────────

    def stop_ninfer(self, container_name: str, timeout: int = 30) -> dict:
        """Stop NInfer container via `docker stop` (graceful SIGTERM→SIGKILL).

        Migrated from base._stop_docker_container — preserves the full guard
        (None-name / TimeoutExpired / FileNotFoundError / non-zero rc) and the
        fix-1 log.warning on each failure branch. No `docker rm`: ninfer's
        `docker run --rm` auto-removes on stop.
        """
        if not container_name:
            msg = "docker deployment has no container_name — cannot stop"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        log.info("Stopping NInfer container: %s", container_name)
        try:
            result = subprocess.run(
                ["docker", "stop", container_name],
                timeout=timeout, capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            msg = f"docker stop {container_name} timed out"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        except FileNotFoundError:
            msg = "docker not found on PATH"
            log.warning(msg)
            return {"status": "warning", "message": msg}
        if result.returncode == 0:
            self._set_ninfer_pid(None)
            self._set_ninfer_container(None)
            return {"status": "ok", "message": f"Container {container_name} stopped"}
        stderr_fragment = result.stderr.decode()[:200] if result.stderr else ""
        msg = f"docker stop exit {result.returncode}: {stderr_fragment}"
        log.warning(msg)
        self._set_ninfer_pid(None)
        self._set_ninfer_container(None)
        return {"status": "warning", "message": msg}

    # ─── liveness ───────────────────────────────────────────────

    def is_ninfer_alive(self, port: int) -> bool:
        return check_http_status(f"http://localhost:{port}/v1/models", timeout=2) == "✅"
