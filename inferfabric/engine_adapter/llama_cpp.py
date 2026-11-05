
"""LlamaCppAdapter — llama-server (CPU) adapter.
Health: /health returns {"status":"ok"}  |  Metrics: not exposed
"""
from __future__ import annotations
from typing import TYPE_CHECKING
from inferfabric.engine_adapter.base import EngineAdapter
from inferfabric.engine_adapter import register
from inferfabric.health import check_http_status
if TYPE_CHECKING:
    from inferfabric.config import ModelConfig

class LlamaCppAdapter(EngineAdapter):
    def __init__(self, process_manager=None):
        self._proc = process_manager

    @property
    def engine_type(self) -> str:
        return "llama_cpp"

    def check_health(self, model: ModelConfig) -> str:
        if not model.llama_cpp:
            return "?"
        return check_http_status(f"http://localhost:{model.llama_cpp.port}/health")

    def get_context_window(self, model: ModelConfig) -> int | None:
        if not model.llama_cpp:
            return None
        return model.llama_cpp.context_size

    def validate_config(self, model: ModelConfig) -> list[str]:
        issues = []
        if not model.llama_cpp:
            return ["Missing llama_cpp config block"]
        if not model.llama_cpp.model_path:
            issues.append("llama_cpp.model_path is empty")
        if model.llama_cpp.port <= 0:
            issues.append(f"Invalid llama_cpp.port: {model.llama_cpp.port}")
        return issues

    def start(self, model: ModelConfig) -> dict:
        """Start llama_cpp via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set — call inject ._proc on the adapter instance first")
        cfg = getattr(model, 'llama_cpp')
        return self._proc.start_llama_cpp(cfg)

    def stop(self, model: ModelConfig) -> dict:
        """Stop llama_cpp via ProcessManager delegation."""
        if self._proc is None:
            raise RuntimeError("ProcessManager not set")
        cfg = getattr(model, 'llama_cpp')
        return self._proc.stop_llama_cpp(port=cfg.port)

    def is_alive(self, model: ModelConfig) -> bool:
        return self.check_health(model) == "\u2705"

    def get_metrics_flags(self, model: ModelConfig) -> list[str]:
        return []  # llama-server does not expose Prometheus metrics

    def fetch_engine_metrics(self, model: ModelConfig) -> dict | None:
        return None  # not supported


    def get_port(self, model: ModelConfig) -> int | None:
        return model.llama_cpp.port if model.llama_cpp else None

    def get_pid_state_key(self) -> str | None:
        return None

register("llama_cpp", LlamaCppAdapter)
