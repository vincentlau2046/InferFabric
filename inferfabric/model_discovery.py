"""
inferfabric/model_discovery.py — Model discovery and auto-deployment.

Extracted from manager.py (v4.1 P1): pure-function module for scanning
local models and auto-generating YAML configs.

All functions are self-contained (no state mutation beyond file I/O),
making them trivially testable.
"""

import re
import subprocess
import logging
from pathlib import Path

log = logging.getLogger("inferfabric")


def get_model_type(model: dict) -> str:
    """Determine the framework/type string for a discovered model.

    Used to classify discovered models for auto-deployment.
    """
    return model.get("type", "vllm")


def discover_local_models(models_dir: Path) -> dict:
    """Scan ~/models/, ~/ComfyUI/models/, and Ollama for unconfigured models.

    Returns models grouped by framework: vllm, ollama, llama_cpp, comfyui.
    Each model has a 'framework' field for frontend grouping.

    Args:
        models_dir: Path to the models YAML directory (for checking configured models).

    Returns:
        {"discovered": [dict, ...], "configured": [str, ...]}
    """
    discovered = []
    configured = []
    configured_dirs = set()
    configured_ollama_refs = set()

    # Load configured models from YAML files
    from .config import load_models
    models = load_models(models_dir)
    configured = sorted(models.keys())

    for m in models.values():
        if hasattr(m, "is_vllm") and m.is_vllm and hasattr(m, "vllm") and m.vllm and m.vllm.model_dir:
            configured_dirs.add(m.vllm.model_dir)
        if hasattr(m, "is_ollama") and m.is_ollama and hasattr(m, "ollama") and m.ollama:
            configured_ollama_refs.add(m.ollama.model_ref)

    # ── vLLM models (~/models/ with config.json) ──
    models_base = Path.home() / "models"
    if models_base.exists():
        def _scan_dirs(parent):
            return [c for c in sorted(parent.glob("*")) if c.is_dir() and not c.name.startswith(".")]

        level1 = _scan_dirs(models_base)
        level2 = [s for d1 in level1 for s in _scan_dirs(d1)]
        level3 = [s for d2 in level2 for s in _scan_dirs(d2)]

        for scan_dir in level1 + level2 + level3:
            if scan_dir.name in configured_dirs:
                continue
            config_json = scan_dir / "config.json"
            if config_json.exists():
                try:
                    size_mb = sum(f.stat().st_size for f in scan_dir.rglob("*") if f.is_file()) // (1024 * 1024)
                except Exception:
                    size_mb = 0
                discovered.append({
                    "name": scan_dir.name, "type": "vllm", "framework": "vllm", "size_mb": size_mb,
                })
            gguf_files = list(scan_dir.glob("*.gguf"))
            if gguf_files:
                skip = False
                for m in models.values():
                    if (hasattr(m, "is_llama_cpp") and m.is_llama_cpp
                            and hasattr(m, "llama_cpp") and m.llama_cpp):
                        mp = str(Path(m.llama_cpp.model_path).expanduser().parent)
                        if str(scan_dir) == mp or str(scan_dir).startswith(mp + "/"):
                            skip = True
                            break
                if not skip:
                    size_mb = sum(f.stat().st_size for f in gguf_files) // (1024 * 1024)
                    discovered.append({
                        "name": scan_dir.name, "type": "llama_cpp", "framework": "llama_cpp", "size_mb": size_mb,
                    })

    # ── Ollama pulled models (ollama list) ──
    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 2 and parts[0] not in ("NAME", ""):
                    model_ref = parts[0]  # e.g. "llama3.2:1b"
                    size_mb = 0
                    for token in parts[1:]:  # skip model name
                        m = re.match(r'^([\d.]+)(GB|MB|KB|G|M|K)$', token, re.IGNORECASE)
                        if m:
                            try:
                                val = float(m.group(1))
                                unit = m.group(2).upper()
                                if unit.startswith("G"):
                                    size_mb = int(val * 1024)
                                elif unit.startswith("M"):
                                    size_mb = int(val)
                                elif unit.startswith("K"):
                                    size_mb = int(val / 1024)
                            except Exception as e:
                                log.debug("discover_local_models scan error: %s", e)
                            break
                    if model_ref not in configured_ollama_refs:
                        discovered.append({
                            "name": model_ref, "type": "ollama", "framework": "ollama", "size_mb": size_mb,
                        })
    except Exception as e:
        log.debug("discover_local_models scan error: %s", e)

    # ── ComfyUI models (~/ComfyUI/models/) ──
    comfyui_models = Path.home() / "ComfyUI" / "models"
    if comfyui_models.exists():
        for sub in ["checkpoints", "loras", "diffusion_models", "vae", "ipadapter"]:
            subdir = comfyui_models / sub
            if not subdir.exists():
                continue
            for f in sorted(subdir.glob("*.safetensors")):
                name = f.stem
                size_mb = f.stat().st_size // (1024 * 1024)
                discovered.append({
                    "name": name, "type": f"comfyui_{sub.rstrip('s')}", "framework": "comfyui", "size_mb": size_mb,
                })

    return {"discovered": discovered, "configured": configured}


def auto_deploy(name: str, model_type: str, models_dir: Path, existing_models: dict, switch_fn) -> dict:
    """Auto-generate YAML and deploy a discovered model.

    Args:
        name: Model name.
        model_type: Type string (e.g. "vllm", "llama_cpp").
        models_dir: Path to models YAML directory.
        existing_models: Dict of currently loaded models (for port scanning).
        switch_fn: Callable(name) -> dict — the ModelManager.switch() method.

    Returns:
        dict with status message.
    """
    yaml_path = models_dir / f"{name}.yaml"
    if yaml_path.exists():
        return {"status": "already_configured", "message": f"YAML already exists for {name}, running switch"}

    # Find next available port
    used_ports = set()
    for m in existing_models.values():
        if hasattr(m, "port") and m.port:
            used_ports.add(m.port)

    if model_type == "vllm":
        port = max([p for p in used_ports if p >= 8000] + [7999]) + 1
        yaml_content = f"""name: {name}
description: "{name} (auto-deployed)"
type: vllm
gpu_role: shared
vllm:
  model_dir: {name}
  served_name: {name}
  port: {port}
  conda_env: qw36-27b-vllm
  gpu_memory_utilization: 0.83
  max_model_len: 131072
  max_num_seqs: 4
  kv_cache_dtype: auto
"""
    elif model_type.startswith("llama_cpp"):
        port = max([p for p in used_ports if p >= 11435] + [11434]) + 1
        yaml_content = f"""name: {name}
description: "{name} (auto-deployed, llama.cpp)"
type: llama_cpp
gpu_role: none
llama_cpp:
  model_path: ~/models/{name}/
  port: {port}
  threads: 16
  context_size: 16384
  gpu_layers: 0
"""
    else:
        return {"status": "error", "message": f"Unsupported type for auto-deploy: {model_type}"}

    # Atomic write: write to .tmp then rename (POSIX atomic)
    tmp_path = yaml_path.with_suffix(".yaml.tmp")
    try:
        tmp_path.write_text(yaml_content)
        tmp_path.rename(yaml_path)
    except Exception as e:
        # Clean up tmp on failure
        tmp_path.unlink(missing_ok=True)
        return {"status": "error", "message": f"Failed to write YAML: {e}"}
    log.info("Auto-generated YAML (atomic): %s", yaml_path)

    # Reload models and switch
    from .config import load_models
    load_models(models_dir)  # refresh module-level cache
    return switch_fn(name)