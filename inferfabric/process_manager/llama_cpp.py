"""
inferfabric/process_manager/llama_cpp.py — Llama.cpp lifecycle sub-manager.
"""

import os
import time
import shlex
import logging
import subprocess
from pathlib import Path
from typing import Optional

from inferfabric.health import wait_http
from inferfabric.process_manager.base import BaseProcessManager


log = logging.getLogger("inferfabric")


class LlamaCppProcessManager(BaseProcessManager):
    """Llama.cpp / llama-server process lifecycle: start, stop."""

    def _probe_embeddings(self, port: int, timeout: int = 10) -> tuple[bool, str]:
        """受管 embedding 就绪探测（Task 5）：真实 /v1/embeddings 机制探测，
        超越端口健康 —— /health 200 不代表 embedding 端点可用。

        Returns: (ok, detail)。响应必须是含 "data" 的 JSON（OpenAI 形态）。
        """
        import json as _json
        import urllib.request
        url = f"http://127.0.0.1:{port}/v1/embeddings"
        req = urllib.request.Request(
            url, method="POST",
            data=_json.dumps({"input": "pack readiness probe",
                              "model": "probe"}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                body = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:   # 探测失败即就绪失败，不区分异常类型
            return False, str(e)
        if not isinstance(body, dict) or "data" not in body:
            return False, "response missing embeddings data"
        return True, ""

    def _probe_chat_ready(self, port: int, timeout: int = 2) -> tuple[bool, str]:
        """受管 chat 就绪探测（#23 Mac）：真实 /v1/chat/completions 机制探测
        （max_tokens=1），超越端口健康 —— /health 200 不代表 chat 端点可用。

        Returns: (ok, detail)。响应必须是含 "choices" 的 JSON（OpenAI 形态）。
        """
        import json as _json
        import urllib.request
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        req = urllib.request.Request(
            url, method="POST",
            data=_json.dumps({"messages": [{"role": "user", "content": "ping"}],
                              "max_tokens": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}"
                body = _json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:   # 探测失败即就绪失败，不区分异常类型
            return False, str(e)
        if not isinstance(body, dict) or "choices" not in body:
            return False, "response missing chat choices"
        return True, ""

    @staticmethod
    def _kill_process_group(pid: int) -> None:
        """进程组收割（degraded 隔离的清理手段；start_new_session → pgid=pid）。"""
        import signal
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass

    def start_llama_cpp(self, cfg: "LlamaCppConfig", model_type: str = "",
                        *, managed=None) -> dict:
        """Start Llama.cpp / llama.cpp server for a specific model.

        Each model gets its own process with process group isolation.
        Uses llama-server binary (OpenAI-compatible API).

        Task 5（BGE CPU 试点）+ #23 Mac：managed=ResolvedRuntime 时 —— 只认
        包内核准二进制（binary_path 必须在 pack_dir 内，否则
        runtime_identity_mismatch）、loopback 绑定、/health 后按包契约
        （manifest healthcheck）追加机制探测（chat → /v1/chat/completions，
        其余 → /v1/embeddings），失败即收割进程组（start_new_session →
        pgid=pid）。加速按审核 YAML：embedding/rerank 强制 CPU（不透传
        -ngl），chat 透传 cfg.gpu_layers（Mac Metal）；端点 flag 按
        model_type 分派。进程组隔离是 degraded 级别，不声称文件/网络
        容器化（spec §4）。managed=None 行为不变。
        """
        from inferfabric.config import LlamaCppConfig
        model_path = Path(cfg.model_path).expanduser().resolve()
        if not model_path.exists():
            return {"status": "error", "message": f"GGUF model not found: {model_path}"}

        # ── Select llama-server binary by GPU offload mode ──
        # binary_path (pack-bundled engine) takes priority when set;
        # otherwise gpu_layers != 0 → CUDA build (GPU offload),
        # gpu_layers == 0 → CPU-only build (no CUDA runtime overhead)
        llama_cpp_base = Path.home() / "llama-cpp"
        if managed is not None:
            # 核准运行时路径：只认包内 binary_path；包外/缺失一律拒绝
            binary = (Path(cfg.binary_path).expanduser()
                      if getattr(cfg, "binary_path", "") else None)
            pack_dir = Path(managed.pack_dir).resolve() if managed.pack_dir \
                else None
            if binary is None or not binary.exists():
                return {"status": "error",
                        "message": (f"managed pack {managed.pack_id}: "
                                    "binary_path missing — approved runtime "
                                    "path required")}
            if pack_dir is None or pack_dir not in binary.expanduser().resolve().parents:
                return {"status": "error",
                        "reason_code": "runtime_identity_mismatch",
                        "message": (f"managed pack {managed.pack_id}: binary "
                                    f"{binary} outside pack dir — not an "
                                    "approved runtime path")}
            llama_server = binary
        elif getattr(cfg, "binary_path", ""):
            llama_server = Path(cfg.binary_path).expanduser()
            if not llama_server.exists():
                return {"status": "error", "message": f"llama-server not found at {llama_server} (binary_path)"}
        elif cfg.gpu_layers != 0:
            llama_server = llama_cpp_base / "build-cuda" / "bin" / "llama-server"
            if not llama_server.exists():
                return {"status": "error", "message": f"CUDA llama-server not found at {llama_server}"}
        else:
            llama_server = llama_cpp_base / "build" / "bin" / "llama-server"
            if not llama_server.exists():
                # Fallback to conda/PATH for backward compat
                conda_base = Path.home() / "miniconda3"
                conda_bin = conda_base / "bin"
                llama_server = conda_bin / "llama-server"
                if not llama_server.exists():
                    import shutil
                    llama_server_path = shutil.which("llama-server")
                    if llama_server_path:
                        llama_server = Path(llama_server_path)
                    else:
                        return {"status": "error", "message": f"llama-server not found"}

        host = "127.0.0.1" if managed is not None else "0.0.0.0"
        cmd = [
            str(llama_server),
            "-m", str(model_path),
            "--host", host,
            "--port", str(cfg.port),
            "-c", str(cfg.context_size),
            "-t", str(cfg.threads),
        ]
        # Endpoint by model type. managed=None: legacy behavior unchanged
        # (non-rerank appends --embeddings as before). Managed (#23 Mac):
        # dispatch by model_type — chat must NOT get --embeddings
        # (llama.cpp semantics: restrict to embedding use case), while
        # embedding/rerank keep their endpoint flags.
        if managed is not None:
            if model_type == 'rerank':
                cmd.append("--reranking")
            elif model_type == 'embedding':
                cmd.append("--embeddings")
        elif model_type == 'rerank':
            cmd.append("--reranking")
        else:
            # embedding (default) and other types get embeddings endpoint
            cmd.append("--embeddings")
        # Acceleration. managed=None: legacy -ngl passthrough unchanged.
        # Managed: embedding/rerank stay CPU-forced (Task 5 contract, even
        # when the YAML says 99 — pre-Task5 generated YAMLs do); chat
        # trusts the approved YAML's gpu_layers (config_gen: 0 if
        # embedding else 99 — Mac Metal full offload).
        if managed is None:
            if cfg.gpu_layers != 0:
                cmd.extend(["-ngl", str(cfg.gpu_layers)])
        elif model_type in ("embedding", "rerank"):
            # Metal engine defaults -ngl to auto (offload) — omission is
            # NOT CPU force; explicit 0 pins the Task 5 CPU contract even
            # when the YAML says 99
            cmd.extend(["-ngl", "0"])
        elif cfg.gpu_layers > 0:
            cmd.extend(["-ngl", str(cfg.gpu_layers)])
        # Passthrough extra_flags
        if cfg.extra_flags:
            cmd.extend(shlex.split(cfg.extra_flags))

        log.info("Starting llama.cpp: %s", " ".join(cmd[:6]) + "...")
        log_file = self._log_dir / f"llama_cpp_{cfg.port}.log"
        log_file.write_text("")

        env = dict(os.environ)
        # Ensure llama-server can find its shared libs
        llama_server_dir = str(llama_server.parent)
        env["PATH"] = llama_server_dir + ":" + env.get("PATH", "")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        except Exception as e:
            log_fh.close()
            return {"status": "error", "message": f"llama.cpp Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid
        pid_file = self._log_dir / f"llama_cpp_{cfg.port}.pid"
        pid_file.write_text(str(pgid))
        log.info("llama.cpp started: PID=%d, port=%d", pgid, cfg.port)

        # Quick failure detection
        for _ in range(6):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("llama.cpp exited immediately (ret=%d): %s", ret, err[-500:])
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"llama.cpp exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=120)
        if healthy and managed is not None:
            # 受管：/health 之上追加包契约机制探测（#23 Mac）——探针按
            # manifest execution.healthcheck 分派：openai_chat_ready_v1 →
            # /v1/chat/completions，其余 → /v1/embeddings。失败收割进程组
            # （degraded 隔离清理），不留半就绪进程
            if getattr(managed, "healthcheck", "") == "openai_chat_ready_v1":
                probe_kind = "chat"
                ok, detail = self._probe_chat_ready(cfg.port)
            else:
                probe_kind = "embeddings"
                ok, detail = self._probe_embeddings(cfg.port)
            if not ok:
                self._kill_process_group(proc.pid)
                pid_file.unlink(missing_ok=True)
                log.error("managed %s probe failed: %s", probe_kind, detail)
                return {"status": "error",
                        "message": f"{probe_kind} probe failed: {detail}"}
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            self.stop_llama_cpp(cfg.port)
            return {"status": "timeout", "message": "llama.cpp didn't become healthy within 2 minutes"}

    def stop_llama_cpp(self, port: Optional[int] = None):
        """Stop llama.cpp via port-based cleanup."""
        if port:
            self._pkill_by_port(port)
        # Clean up PID file
        for pf in self._log_dir.glob("llama_cpp_*.pid"):
            pf.unlink(missing_ok=True)

    def run_ollama(self, model_ref: str, keep_alive: str = "5m", num_gpu: int = -1) -> dict:
        """Trigger `ollama run` to load/pull a model into the Ollama daemon.

        Pipes empty stdin so the CLI loads the model then exits without
        blocking. `num_gpu` is passed via `OLLAMA_NUM_GPU` env var to control
        GPU offloading layers (e.g., 0 for CPU-only, -1 for auto).
        """
        env = os.environ.copy()
        if num_gpu >= 0:
            env["OLLAMA_NUM_GPU"] = str(num_gpu)

        cmd = ["ollama", "run", model_ref, "--keepalive", keep_alive]
        try:
            result = subprocess.run(
                cmd, input="\n", capture_output=True, text=True, timeout=300, env=env
            )
        except FileNotFoundError:
            return {"status": "error", "message": "ollama CLI not found in PATH"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"ollama run timed out after 300s for {model_ref}"}
        if result.returncode == 0:
            return {"status": "ok", "message": f"Loaded {model_ref} into Ollama daemon"}
        return {
            "status": "error",
            "message": f"ollama run failed: {result.stderr.strip() or result.stdout.strip()}",
        }

    # ─── Llama.cpp ──────────────────────────────────────────────

