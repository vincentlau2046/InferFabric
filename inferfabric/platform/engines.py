"""Per-platform default engine matrix (pure data, import-isolated).

Keys mirror PlatformProfile.os. Values are engine-type names as registered
in the upstream engine-adapter registry (`inferfabric/engine_adapter`).
The matrix documents which engines a platform line supports by default;
model YAMLs may still pin any engine explicitly.
"""

ENGINES: dict[str, list[str]] = {
    # Apple Silicon: Metal llama.cpp is the chat workhorse; no CUDA lines.
    "darwin": ["llama_cpp", "ollama", "tts_server", "asr_server"],
    # x86 + NVIDIA: upstream full matrix.
    "linux": ["vllm", "sglang", "ninfer", "ollama", "llama_cpp",
              "comfyui", "tts_server", "asr_server"],
    # Unknown platforms fall back to the upstream (linux) matrix.
    "other": ["vllm", "sglang", "ninfer", "ollama", "llama_cpp",
              "comfyui", "tts_server", "asr_server"],
}

__all__ = ["ENGINES"]
