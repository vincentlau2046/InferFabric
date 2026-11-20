"""Manifest → ModelConfig 生成 — PK3（Task 7）。

包的正式执行配置由 Pack Manager 生成到受控 models.d 区域，带 managed_by_pack
标记（卸载按 pack_id 定位受控文件）。现有 ModelConfig/loader 承担执行层
（spec §10）：本模块只做映射，不重写生命周期。

- 严格一模型一环境：引擎二进制与模型文件路径全部指向包目录内绝对路径。
- YAML `mode: on_demand` 走现有 loader 消歧（config.py:902-916）→ ledger_mode。
- autostart=False：安装不启动（spec §2.1），首次调用按需拉起。
- port 由调用方分配传入（PackManager 负责空闲端口）。
- 受控 YAML 只经 .part + os.replace 原子落盘，不留截断半成品（终审 F2）。
"""

import os
from dataclasses import replace
from pathlib import Path

from inferfabric.config import LlamaCppConfig, ModelConfig

CAPABILITY_TO_MODEL_TYPE = {
    "llm": "llm", "vl": "vl", "asr": "asr", "tts": "tts",
    "embedding": "embedding", "rerank": "rerank", "ocr": "ocr",
}

# 已实现模板注册表（单一事实源）：installer 预检据此提前拒绝未实现模板，
# 避免下载数 GB 后才在 writing_config 失败（deferred #5）。
KNOWN_TEMPLATES = frozenset({"llama_cpp_chat_v1", "llama_cpp_embedding_v1"})


class ConfigExistsError(ValueError):
    pass


def pack_model_name(manifest) -> str:
    short = manifest.pack_id.split(".", 1)[1]
    return f"pack-{short}-{manifest.pack_version.replace('.', '-')}"


def managed_yaml_path(models_d, manifest) -> Path:
    return Path(models_d) / f"{pack_model_name(manifest)}.yaml"


def _require_managed_dir(pack_dir: Path) -> Path:
    pack_dir = Path(pack_dir)
    if not pack_dir.is_absolute():
        raise ValueError(f"pack_dir must be absolute, got {pack_dir}")
    return pack_dir


def build_model_config(manifest, pack_dir, port: int) -> ModelConfig:
    """模板分发。llama_cpp_chat_v1 为首个实现；新运行时模板在此注册。"""
    tpl = manifest.model_config_template
    if tpl == "llama_cpp_chat_v1":
        return _build_llama_cpp(manifest, pack_dir, port)
    if tpl == "llama_cpp_embedding_v1":
        # Task 5（BGE CPU 试点）：embedding 受管配置 —— CPU（gpu_layers=0）、
        # 无 chat 专用 --jinja；PM 层受管路径再强制 CPU 双保险。
        return _build_llama_cpp(manifest, pack_dir, port, embedding=True)
    raise ValueError(f"unknown model_config_template {tpl!r}")


def _build_llama_cpp(manifest, pack_dir, port: int,
                     embedding: bool = False) -> ModelConfig:
    pack_dir = _require_managed_dir(pack_dir)
    model_art = next(a for a in manifest.artifacts if a.kind == "model")
    engine_art = next((a for a in manifest.artifacts if a.kind == "runtime"),
                      None)
    model_path = pack_dir / "models" / model_art.id
    binary_path = (pack_dir / "runtime" / engine_art.id) if engine_art else ""
    return ModelConfig(
        name=pack_model_name(manifest),
        description=f"capability pack {manifest.pack_id} "
                    f"{manifest.pack_version} ({manifest.license})",
        gpu_role=manifest.gpu_role,
        type="llama_cpp",
        model_type=CAPABILITY_TO_MODEL_TYPE[manifest.capability],
        # Fork parity deltas (anthropic_native / mem_baseline_mb /
        # idle_timeout_min / autostart) land with their own unification
        # entries (F1 / G1 ledger / F9 autostart); upstream peak_vram_mb
        # carries the memory-gate semantics meanwhile.
        peak_vram_mb=manifest.peak_memory_mb,
        ledger_mode=manifest.lifecycle_mode,
        llama_cpp=LlamaCppConfig(
            model_path=str(model_path),
            binary_path=str(binary_path),
            served_name=manifest.model_id,   # proxy 路由名（--alias 同值规范回显）
            port=port,
            threads=8,
            context_size=8192,
            gpu_layers=0 if embedding else 99,
            extra_flags=(f"--alias {manifest.model_id}" if embedding
                         else f"--alias {manifest.model_id} --jinja"),
        ),
    )


def write_managed_yaml(cfg: ModelConfig, models_d, manifest) -> Path:
    """写受控 YAML（含 managed_by_pack 标记）；同名已存在拒绝覆盖。"""
    models_d = Path(models_d)
    models_d.mkdir(parents=True, exist_ok=True)
    path = managed_yaml_path(models_d, manifest)
    if path.exists():
        raise ConfigExistsError(f"managed yaml already exists: {path}")
    d = {
        "name": cfg.name,
        "description": cfg.description,
        "type": cfg.type,
        "gpu_role": cfg.gpu_role,
        "model_type": cfg.model_type,
        "peak_vram_mb": cfg.peak_vram_mb,
        "served_name": manifest.model_id,   # 文档性；路由名在 llama_cpp.served_name
        # ledger lifecycle (manifest.lifecycle_mode) intentionally not written:
        # upstream `mode` is a gpu_role alias (G1 dual-semantics lands with the
        # ledger entry) — writing on_demand here would corrupt gpu_role.
        "managed_by_pack": {"marker_version": 1,
                            "pack_id": manifest.pack_id,
                            "pack_version": manifest.pack_version},
        "llama_cpp": {
            "model_path": cfg.llama_cpp.model_path,
            "binary_path": cfg.llama_cpp.binary_path,
            "served_name": cfg.llama_cpp.served_name,
            "port": cfg.llama_cpp.port,
            "threads": cfg.llama_cpp.threads,
            "context_size": cfg.llama_cpp.context_size,
            "gpu_layers": cfg.llama_cpp.gpu_layers,
            "extra_flags": cfg.llama_cpp.extra_flags,
        },
    }
    import yaml
    part = path.with_name(path.name + ".part")
    with open(part, "w", encoding="utf-8") as f:
        f.write(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)   # 原子落盘：目标文件要么完整要么不存在
    return path
