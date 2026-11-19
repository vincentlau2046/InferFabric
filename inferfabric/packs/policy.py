"""4B/8GB 策略设置项与参数门禁 — PK0（Task 2）。

spec §9 裁决：
- 默认 max_model_parameters=4B、system_reserved_memory=8192 MiB 不变；
  高级设置可调整，仅影响后续安装/启动准入，不自动重装、不自动并行启动。
- 参数量门禁仅作用于对话类 LLM/VL；ASR/TTS/Embedding/Rerank/OCR 按内存
  门禁（安装预检的 peak_memory_mb 对照，在 installer），不设参数门槛。
"""

import json
import os
from dataclasses import dataclass, asdict

DEFAULT_MAX_MODEL_PARAMETERS_B = 4
DEFAULT_SYSTEM_RESERVED_MEMORY_MB = 8192


@dataclass(frozen=True)
class PackPolicy:
    max_model_parameters_b: int = DEFAULT_MAX_MODEL_PARAMETERS_B
    system_reserved_memory_mb: int = DEFAULT_SYSTEM_RESERVED_MEMORY_MB


class PolicyStore:
    """PackPolicy 的 JSON 持久化；路径由调用方注入（生产 ~/.inferfabric/packs/policy.json）。"""

    def __init__(self, path):
        self._path = path

    def load(self) -> PackPolicy:
        try:
            with open(self._path, encoding="utf-8") as f:
                d = json.load(f)
            p = PackPolicy(max_model_parameters_b=int(d["max_model_parameters_b"]),
                           system_reserved_memory_mb=int(d["system_reserved_memory_mb"]))
            self._validate(p)
            return p
        except (OSError, ValueError, KeyError, TypeError):
            return PackPolicy()

    def save(self, policy: PackPolicy) -> None:
        self._validate(policy)
        os.makedirs(os.path.dirname(str(self._path)) or ".", exist_ok=True)
        part = f"{self._path}.part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(asdict(policy), f, ensure_ascii=False, indent=1)
        os.replace(part, self._path)

    @staticmethod
    def _validate(p: PackPolicy) -> None:
        if p.max_model_parameters_b < 1:
            raise ValueError("max_model_parameters_b must be >= 1")
        if p.system_reserved_memory_mb < 1:
            raise ValueError("system_reserved_memory_mb must be >= 1")


def check_param_gate(manifest, policy: PackPolicy) -> str | None:
    """参数量门禁：仅对话类 LLM/VL；返回违规原因或 None。"""
    if manifest.capability not in ("llm", "vl"):
        return None
    if manifest.parameter_count_b > policy.max_model_parameters_b:
        return (f"parameter_count_b {manifest.parameter_count_b} > "
                f"max {policy.max_model_parameters_b}")
    return None
