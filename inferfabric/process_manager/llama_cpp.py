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

    def start_llama_cpp(self, cfg: "LlamaCppConfig", model_type: str = "") -> dict:
        """Start Llama.cpp / llama.cpp server for a specific model.

        Each model gets its own process with process group isolation.
        Uses llama-server binary (OpenAI-compatible API).
        """
        from inferfabric.config import LlamaCppConfig
        model_path = Path(cfg.model_path).expanduser().resolve()
        if not model_path.exists():
            return {"status": "error", "message": f"GGUF model not found: {model_path}"}

        # ── Select llama-server binary by GPU offload mode ──
        # gpu_layers != 0 → CUDA build (GPU offload)
        # gpu_layers == 0 → CPU-only build (no CUDA runtime overhead)
        llama_cpp_base = Path.home() / "llama-cpp"
        if cfg.gpu_layers != 0:
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

        cmd = [
            str(llama_server),
            "-m", str(model_path),
            "--host", "0.0.0.0",
            "--port", str(cfg.port),
            "-c", str(cfg.context_size),
            "-t", str(cfg.threads),
        ]
        # Enable endpoint by model type
        if model_type == 'rerank':
            cmd.append("--reranking")
        else:
            # embedding (default) and other types get embeddings endpoint
            cmd.append("--embeddings")
        if cfg.gpu_layers != 0:
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

