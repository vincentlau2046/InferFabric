"""
inferfabric/config.py — Configuration, constants, and model definitions.

v4.0: Profile concept eliminated. Models are self-describing plugins in models.d/.
Each YAML file declares its own mode (exclusive/shared) and resource requirements.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import json
import yaml
import hashlib
import dataclasses
import logging
import threading

log = logging.getLogger("inferfabric")


class ConfigError(ValueError):
    """Raised on invalid model configuration."""


# Environment variable keys that extra_env must not override
_PROTECTED_ENV_KEYS = frozenset({"PATH", "HOME", "CONDA_DEFAULT_ENV", "CUDA_VISIBLE_DEVICES", "PYTHONPATH", "LD_LIBRARY_PATH"})

# ─── Path Constants ──────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
MODELS_DIR = BASE_DIR / "models.d"
IFF_DATA_DIR = Path.home() / ".inferfabric"
DEFAULT_STATE_DB = IFF_DATA_DIR / "state.db"
DEFAULT_REQUEST_LOG_DB = IFF_DATA_DIR / "request_log.db"
DEFAULT_LOG_DIR = IFF_DATA_DIR / "logs"
# 场景调优应用层（iff tune 写入，机器本地状态，不进 git）：
# {model: {active_preset, overrides}} → load_models overlay 合并进引擎块
APPLIED_SCENARIOS_FILE = IFF_DATA_DIR / "active_scenarios.yaml"
GPU_LOCK_PATH = Path("/tmp/inferfabric_gpu.lock")
MODEL_BASE = Path.home() / "models"
CONDA_ENVS = Path.home() / "miniconda3" / "envs"
COMFYUI_DIR = Path.home() / "ComfyUI"

# ─── Process Management Constants ────────────────────────────────

STOP_SIGTERM_TIMEOUT = 10       # seconds to wait after SIGTERM before SIGKILL
VLLM_STARTUP_CHECK_INTERVAL = 0.5  # seconds between startup checks
VLLM_STARTUP_CHECK_ROUNDS = 20  # 10 seconds total for immediate-failure detection
HEALTH_CHECK_TIMEOUT = 300      # 5 minutes default for vLLM to become healthy (overridden by model startup_timeout)
GPU_FREE_TIMEOUT = 30           # seconds to wait for GPU memory release
GPU_FREE_THRESHOLD_MB = 2048    # MB below which GPU is considered "free"

# P1-6: Auto-clear GPU CUDA state on service transitions
# When True, calls nvidia-smi reset/toggle after GPU-bound services stop
# and before exclusive models start, to mitigate memory fragmentation.
# Set False if fragmentation is not observed or if resets interfere with
# other GPU workloads (e.g., display rendering on the same GPU).
GPU_AUTO_CLEAR_CUDA_STATE = True


# ─── Data Classes ────────────────────────────────────────────────

@dataclass
class SleepModeConfig:
    """vLLM sleep mode (L2 only: discard weights, wake needs reload 3-6s).

    Requires VLLM_SERVER_DEV_MODE=1 + --enable-sleep-mode at startup.
    """
    enabled: bool = False


@dataclass
class VLLMConfig:
    model_dir: str
    served_name: str
    port: int
    conda_env: str
    max_model_len: int
    gpu_memory_utilization: float
    max_num_seqs: int = 4
    kv_cache_dtype: str = "auto"
    kv_offloading_size: Optional[float] = None
    kv_offloading_backend: str = "native"
    speculative_config: Optional[str] = None
    extra_flags: str = ""
    sleep_mode: Optional[SleepModeConfig] = None
    startup_timeout: int = 0  # seconds for health check; 0 = use global HEALTH_CHECK_TIMEOUT
    extra_env: dict[str, str] = field(default_factory=dict)  # inject into subprocess env
    docker_image: str = "vllm/vllm-openai:v0.24.0"  # vLLM 0.24 pinned (CLAUDE.md: do not upgrade)

    def build_cmd(self) -> list[str]:
        """Build vLLM command. JSON args stay as single elements."""
        model_path = MODEL_BASE / self.model_dir
        flags = [
            "vllm", "serve", str(model_path),
            "--served-model-name", self.served_name,
            "--max-model-len", str(self.max_model_len),
            "--gpu-memory-utilization", str(self.gpu_memory_utilization),
            "--max-num-seqs", str(self.max_num_seqs),
            "--kv-cache-dtype", self.kv_cache_dtype,
            "--port", str(self.port),
            "--host", "0.0.0.0",
        ]
        if self.kv_offloading_size is not None:
            flags.extend([
                "--kv-offloading-size", str(self.kv_offloading_size),
                "--kv-offloading-backend", self.kv_offloading_backend,
            ])
        if self.speculative_config:
            flags.extend(["--speculative-config", self.speculative_config])
        if self.extra_flags:
            import shlex
            flags.extend(shlex.split(self.extra_flags))
        return flags

    def build_docker_cmd(self, container_name: str) -> list[str]:
        """Build docker run command for vLLM serving.

        container_name threaded from ModelConfig.container_name (single source).
        Mirrors SGLangConfig.build_docker_cmd; translates the conda start_vllm env
        handling (PYTORCH_CUDA_ALLOC_CONF expandable_segments, sleep-mode
        VLLM_SERVER_DEV_MODE, extra_env) into docker -e flags so the container
        behaves like the conda launch.
        """
        model_path = MODEL_BASE / self.model_dir
        # build_cmd() → ["vllm", "serve", path, ...]; --entrypoint vllm supplies
        # the binary, so drop the leading "vllm" and keep "serve" + args.
        container_cmd = self.build_cmd()[1:]

        docker_flags = [
            "docker", "run", "--rm",
            "--gpus", "all",
            "--ipc=host",
            "--ulimit", "memlock=-1",
            "--ulimit", "stack=67108864",
            "-p", f"{self.port}:{self.port}",
            "-v", f"{model_path}:{model_path}",
            "-v", f"{MODEL_BASE}:/models",
            "-v", f"{Path.home() / '.cache/huggingface'}:/root/.cache/huggingface",
            "--name", container_name,
            "--entrypoint", "vllm",
        ]

        # Env translation (mirror start_vllm conda env handling)
        has_kv_offload = "--kv-offloading-size" in " ".join(container_cmd)
        if not has_kv_offload:
            docker_flags.extend(["-e", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"])

        if self.sleep_mode and self.sleep_mode.enabled:
            # extra_env must not override VLLM_SERVER_DEV_MODE (sleep mode requires it)
            if "VLLM_SERVER_DEV_MODE" in self.extra_env:
                raise ConfigError(
                    f"extra_env overrides VLLM_SERVER_DEV_MODE for {self.served_name} — "
                    f"sleep mode will not work. Remove VLLM_SERVER_DEV_MODE from extra_env "
                    f"or disable sleep_mode."
                )
            docker_flags.extend(["-e", "VLLM_SERVER_DEV_MODE=1"])
            container_cmd.append("--enable-sleep-mode")

        if self.extra_env:
            for k, v in self.extra_env.items():
                docker_flags.extend(["-e", f"{k}={v}"])

        return docker_flags + [self.docker_image] + container_cmd


@dataclass
class SGLangConfig:
    """SGLang inference server config. RadixAttention + NVFP4 native support.

    Structured fields are emitted as CLI flags in build_cmd().
    Any flag not covered should go into extra_flags.
    """
    model_dir: str
    served_name: str
    port: int
    conda_env: str = ""
    mem_fraction: float = 0.85
    docker_image: str = "lmsysorg/sglang:latest"

    # -- structured flags (migrated from extra_flags) --
    context_length: int | None = None
    max_running_requests: int = 8
    cpu_offload_gb: int = 0
    enable_lmcache: bool = False
    language_model_only: bool = False
    reasoning_parser: str = ""
    tool_call_parser: str = ""

    extra_flags: str = ""
    startup_timeout: int = 0
    extra_env: dict[str, str] = field(default_factory=dict)

    @property
    def health_url(self) -> str:
        return f"http://localhost:{self.port}/health"

    def build_cmd(self) -> list[str]:
        """Build SGLang serve command with structured fields + extra_flags."""
        model_path = MODEL_BASE / self.model_dir
        flags = [
            "sglang", "serve", str(model_path),
            "--served-model-name", self.served_name,
            "--mem-fraction-static", str(self.mem_fraction),
            "--port", str(self.port),
            "--host", "0.0.0.0",
            "--tp-size", "1",
        ]
        if self.max_running_requests:
            flags.extend(["--max-running-requests", str(self.max_running_requests)])
        if self.context_length:
            flags.extend(["--context-length", str(self.context_length)])
        if self.language_model_only:
            flags.append("--language-model-only")
        if self.reasoning_parser:
            flags.extend(["--reasoning-parser", self.reasoning_parser])
        if self.tool_call_parser:
            flags.extend(["--tool-call-parser", self.tool_call_parser])
        if self.cpu_offload_gb > 0:
            flags.extend(["--cpu-offload-gb", str(self.cpu_offload_gb)])
        if self.enable_lmcache:
            flags.append("--enable-lmcache")
        if self.extra_flags:
            import shlex
            flags.extend(shlex.split(self.extra_flags))
        return flags

    def build_docker_cmd(self, container_name: str) -> list[str]:
        """Build docker run command for SGLang serving.

        container_name threaded from ModelConfig.container_name (single source).
        """
        import shlex
        model_path = MODEL_BASE / self.model_dir
        container_cmd = self.build_cmd()
        docker_flags = [
            "docker", "run", "--rm",
            "--gpus", "all",
            "--ipc=host",
            "--ulimit", "memlock=-1",
            "--ulimit", "stack=67108864",
            "-p", f"{self.port}:{self.port}",
            "-v", f"{model_path}:{model_path}",
            "-v", f"{MODEL_BASE}:/models",
            "-v", f"{Path.home() / '.cache/huggingface'}:/root/.cache/huggingface",
            "--name", container_name,
        ]
        if self.extra_env:
            for k, v in self.extra_env.items():
                docker_flags.extend(["-e", f"{k}={v}"])
        # Replace sglang binary path with full path inside container
        container_cmd[0] = "/usr/local/bin/sglang"
        return docker_flags + [self.docker_image] + container_cmd


@dataclass
class ComfyUIConfig:
    """ComfyUI configuration. Supports both native Python and legacy script modes."""
    conda_env: str = "comfyui"
    port: int = 8188
    working_dir: str = ""
    health_url: str = ""
    extra_flags: str = "--cache-none --enable-manager"
    # Legacy fallback (deprecated — native mode preferred)
    startup_script: str = ""
    stop_script: str = ""

    @property
    def use_native(self) -> bool:
        """True if we should use native Python process management."""
        return bool(self.conda_env and not self.startup_script)

    @property
    def resolved_working_dir(self) -> Path:
        wd = self.working_dir or str(COMFYUI_DIR)
        return Path(wd).expanduser().resolve()


@dataclass
class TTSConfig:
    """TTS server configuration — OpenAI-compatible /v1/audio/speech API.

    Manages a standalone TTS process (e.g. Qwen3-TTS-Openai-Fastapi)
    with Conda-based deployment, process group isolation, and
    health check via HTTP endpoint.
    """
    conda_env: str
    port: int = 8880
    working_dir: str = ""
    health_url: str = ""
    health_check_timeout: int = 180  # seconds; TTS warmup can be slow
    start_cmd: str = "python -m api.main"
    extra_env: dict[str, str] = field(default_factory=dict)  # inject into subprocess env

    @property
    def resolved_working_dir(self) -> Path:
        wd = self.working_dir or str(Path.home() / "services" / "tts")
        return Path(wd).expanduser().resolve()


@dataclass
class ASRConfig:
    """ASR server configuration — OpenAI-compatible /v1/audio/transcriptions API.

    Manages a standalone ASR process (e.g. FunASR funasr-server)
    with Conda-based deployment, process group isolation, and
    health check via HTTP endpoint.
    """
    conda_env: str
    port: int = 8881
    working_dir: str = ""
    health_url: str = ""
    health_check_timeout: int = 120  # seconds; ASR models load faster than TTS
    start_cmd: str = "funasr-server --model sensevoice --device cuda"
    extra_env: dict[str, str] = field(default_factory=dict)  # inject into subprocess env

    @property
    def resolved_working_dir(self) -> Path:
        wd = self.working_dir or str(Path.home() / "services" / "asr")
        return Path(wd).expanduser().resolve()


@dataclass
class OllamaModelConfig:
    """Ollama 模型引用 — 不管理 daemon，只声明模型名."""
    model_ref: str  # "llama3.1:8b"
    keep_alive: str = "5m"
    num_gpu: int = -1  # -1=auto, 0=CPU only, N=GPU layers


@dataclass
class LlamaCppConfig:
    """Llama.cpp / llama.cpp 独立推理进程."""
    model_path: str     # GGUF 文件路径
    port: int = 11435
    threads: int = 8
    context_size: int = 8192
    gpu_layers: int = 0  # 0=CPU only, -1=all GPU, N=部分
    extra_flags: str = ""  # 透传给 llama-server 的额外参数


@dataclass
class NInferConfig:
    """NInfer Docker inference engine."""
    served_name: str = ""  # proxy 路由名，为空时 fallback 到 model name
    model_id: str = "qwen38-27b-text"  # NInfer --model-id 参数，覆盖 artifact identity，使 proxy 模型名匹配
    weight_path: str = ""
    docker_image: str = "ninfer:qwen38-nvfp4-cuda13"
    port: int = 8007
    container_name: str = ""
    max_context: int = 262144    # 单请求最大上下文（tokens）
    kv_capacity: int = 262144    # KV cache 池大小（tokens）
    kv_dtype: str = "int8"      # KV cache 精度: int8 | bf16
    weight_precision: str = "NVFP4"  # 权重量化精度
    max_concurrency: int = 3
    default_max_tokens: int = 32000
    pending_timeout_ms: int = 600000
    prefill_chunk: int = 4096
    startup_timeout: int = 120
    enable_mtp: bool = True
    draft_tokens: int = 4
    enable_lm_head_draft: bool = False
    vision: bool = False   # VL/多模态: 为 true 时追加 --vision (启用视觉塔+media)
    media_cache_mib: int = 1024    # media 预处理复用缓存 (MiB); 0 关闭跨请求复用
    media_live_mib: int = 2048     # media 在线 BF16 patch 上限 (MiB); 视觉下 minimum ~384MiB, 不可为 0
    extra_flags: str = ""
    log_file: str = ""


@dataclass
class OllamaDaemonConfig:
    """Ollama 守护进程 — 基础设施服务."""
    port: int = 11434
    health_url: str = "http://localhost:11434"
    data_dir: str = ""


class PortPool:
    """Port range conventions for different service types.

    Each range reserves 10 ports (e.g. 11440-11449 for embeddings).
    Ports are configured statically in model YAML files; this class
    documents the convention for administrators.
    """
    EMBEDDING_START = 11440
    # Future ranges: LLAMA_CPP_START = 11430, etc.


# Modality derived from model_type; used when YAML omits explicit modality.
MODEL_TYPE_TO_MODALITY = {
    "llm": "text",
    "vl": "text-vision",
    "omni": "multimodal",
    "ocr": "text-vision",
    "aigc": "aigc",
    "embedding": "embedding",
    "rerank": "rerank",
    "infra": "infra",
    "tts": "tts",
    "asr": "asr",
}


@dataclass
class ModelConfig:
    """A deployable model/service — one per YAML in models.d/.

    Core attributes:
      name:        Unique identifier (must match YAML filename stem)
      description: Human-readable description
      gpu_role:   'exclusive' (GPU fully locked) | 'shared' (coexists with other shared services) | 'none' (CPU-only)
      type:        'vllm' | 'comfyui' | 'ollama' | 'llama_cpp' | 'ollama_daemon'
      vllm:        VLLMConfig if type='vllm'
      comfyui:     ComfyUIConfig if type='comfyui'
      ollama:      OllamaModelConfig if type='ollama'
      llama_cpp:  LlamaCppConfig if type='llama_cpp'
      ollama_daemon: OllamaDaemonConfig if type='ollama_daemon'
      model_type:  'llm' | 'vl' | 'omni' | 'ocr' | 'aigc' | 'embedding' | 'rerank' | 'infra' — capability classification
      quantization: quantization format string (e.g. 'NVFP4', 'GPTQ-4bit', 'Q8_0')
    """
    name: str
    description: str
    gpu_role: str = "exclusive"  # 'exclusive' | 'shared' | 'none'

    @property
    def mode(self) -> str:
        """Alias for gpu_role for backward compatibility."""
        return self.gpu_role

    @property
    def resolved_modality(self) -> str:
        """Effective modality: explicit value if set, else derived from model_type."""
        if self.modality:
            return self.modality
        return MODEL_TYPE_TO_MODALITY.get(self.model_type, "text")

    type: str = "vllm"  # 'vllm' | 'sglang' | 'comfyui' | 'ollama' | 'llama_cpp' | 'ollama_daemon' | 'tts_server' | 'asr_server'
    deployment: str = ""  # '' (auto-derive) | 'docker' | 'conda' | 'process' — 部署方式显式声明
    vllm: Optional[VLLMConfig] = None
    sglang: Optional[SGLangConfig] = None
    comfyui: Optional[ComfyUIConfig] = None
    ollama: Optional[OllamaModelConfig] = None
    llama_cpp: Optional[LlamaCppConfig] = None
    ollama_daemon: Optional[OllamaDaemonConfig] = None
    ninfer: Optional[NInferConfig] = None
    tts: Optional[TTSConfig] = None
    asr: Optional[ASRConfig] = None
    typical_vram_pct: float = 0.0
    peak_vram_mb: int = 0  # measured peak VRAM + safety margin; 0 = unknown/unchecked
    model_type: str = "llm"  # 'llm' | 'vl' | 'omni' | 'ocr' | 'aigc' | 'embedding' | 'rerank' | 'infra' | 'tts' | 'asr'
    modality: str = ""  # derived from model_type if empty; 'text' | 'text-vision' | 'multimodal' | 'aigc' | 'embedding' | 'rerank' | 'infra' | 'tts' | 'asr'
    quantization: str = ""  # quantization format: 'NVFP4', 'GPTQ-4bit', 'Q8_0', etc.

    # R7: 多副本端口列表（如 [8002, 8003]；为空时使用 type 对应 config 的 port）
    replicas: list[int] = field(default_factory=list)

    # 场景预设调优（iff tune），三层分离：
    # 1) 场景定义 models.d/scenarios.yaml（侧车，手动配置；同名场景覆盖模型 YAML 内联 `presets:`）
    # 2) 应用层 APPLIED_SCENARIOS_FILE（机器本地，只由 tune 写入，
    #    {model: {active_preset, overrides}}，load_models overlay 合并进引擎块）
    # 3) 模型 YAML 对 tune 工具链只读（启动真相源，注释永不被工具链破坏）
    presets: dict = field(default_factory=dict)
    active_preset: str = ""  # 当前应用场景（由应用层 overlay 填充，空 = 未应用）
    yaml_path: str = ""  # 源 YAML 绝对路径（load_models 填充），供读"纯 YAML 值"
    # 注：物理 KV 池上限不是独立字段——就是各模型 YAML 引擎块里的固定
    # kv_capacity（TXT=600000 / VL=410000），tune 超卖% 以它为参照，不按场景改。

    # Fields excluded from config hash (runtime / non-startup)
    _HASH_EXCLUDE_FIELDS = frozenset({"typical_vram_pct", "peak_vram_mb", "startup_timeout",
                                      "presets", "active_preset", "yaml_path"})

    def config_hash(self) -> str:
        """Deterministic hash of all config fields that affect startup behavior.

        Excludes _HASH_EXCLUDE_FIELDS and None values.  Used for drift detection
        so that a running service is automatically restarted when its YAML changes.
        """
        payload = {}
        for f in dataclasses.fields(self):
            if f.name.startswith("_"):
                continue
            if f.name in self._HASH_EXCLUDE_FIELDS:
                continue
            val = getattr(self, f.name)
            if val is None:
                continue
            # Recurse into nested dataclasses (VLLMConfig, ComfyUIConfig, etc.)
            if dataclasses.is_dataclass(val) and not isinstance(val, type):
                val = dataclasses.asdict(val)
                # Exclude runtime-only fields from nested dataclasses
                for excl in ("startup_timeout", "health_check_timeout"):
                    val.pop(excl, None)
            payload[f.name] = val
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def port(self) -> Optional[int]:
        """Unified port accessor — eliminates per-backend if/else in proxy."""
        if self.vllm:
            return self.vllm.port
        if self.sglang:
            return self.sglang.port
        if self.tts:
            return self.tts.port
        if self.asr:
            return self.asr.port
        if self.ollama_daemon:
            return self.ollama_daemon.port
        if self.ollama:
            return 11434  # Ollama daemon fixed port
        if self.llama_cpp:
            return self.llama_cpp.port
        if self.comfyui:
            return self.comfyui.port
        if self.ninfer:
            return self.ninfer.port
        return None

    @property
    def served_name(self) -> Optional[str]:
        """Unified served_name for proxy routing."""
        if self.vllm:
            return self.vllm.served_name
        if self.sglang:
            return self.sglang.served_name
        if self.ollama:
            return self.ollama.model_ref
        if self.llama_cpp:
            return self.name
        if self.ninfer:
            return self.ninfer.served_name or self.name
        return self.name

    # type → ModelConfig 字段名（tts_server/asr_server 例外，字段是 tts/asr）
    _ENGINE_ATTR = {
        "vllm": "vllm", "sglang": "sglang", "comfyui": "comfyui",
        "ollama": "ollama", "llama_cpp": "llama_cpp", "ollama_daemon": "ollama_daemon",
        "ninfer": "ninfer", "tts_server": "tts", "asr_server": "asr",
    }

    @property
    def engine_config(self) -> Optional[object]:
        """引擎配置块（type 对应的嵌套 dataclass），无则 None。

        tune 通过它读写场景字段——不关心是哪个引擎，字段名就是 config 的键。
        """
        attr = self._ENGINE_ATTR.get(self.type)
        return getattr(self, attr, None) if attr else None

    @property
    def container_name(self) -> Optional[str]:
        """Docker 容器名（docker 部署时用）；conda/process 返回 None。

        统一 ninfer/sglang 两处散落的容器名推导，供 adapter.stop 复用。
        """
        if self.resolved_deployment != "docker":
            return None
        if self.ninfer:
            return self.ninfer.container_name or f"ninfer-{self.ninfer.port}"
        if self.sglang:
            return f"sglang-{self.sglang.served_name}"
        if self.vllm:
            return f"vllm-{self.vllm.served_name}"
        return None

    @property
    def needs_gpu(self) -> bool:
        return self.gpu_role != "none"

    @property
    def is_exclusive(self) -> bool:
        return self.gpu_role == "exclusive"

    @property
    def resolved_deployment(self) -> str:
        """Effective deployment: explicit value if set, else derived from engine type.

        推导规则保证现有 YAML（未写 deployment）行为不变。
        未来 docker+vllm 只需在 YAML 写 deployment: docker 即可启用 docker 路径。
        """
        if self.deployment in ("docker", "conda", "process"):
            return self.deployment
        if self.type in ("ninfer", "sglang"):
            return "docker"
        if self.type in ("vllm", "comfyui", "tts_server", "asr_server"):
            return "conda"
        if self.type in ("llama_cpp", "ollama_daemon"):
            return "process"
        return "process"

    @property
    def is_shared(self) -> bool:
        return self.gpu_role == "shared"

    @property
    def is_gpu_none(self) -> bool:
        return self.gpu_role == "none"

    @property
    def is_vllm(self) -> bool:
        return self.type == "vllm" and self.vllm is not None

    @property
    def is_sglang(self) -> bool:
        return self.type == "sglang" and self.sglang is not None

    @property
    def is_comfyui(self) -> bool:
        return self.type == "comfyui" and self.comfyui is not None

    @property
    def is_ollama(self) -> bool:
        return self.type == "ollama" and self.ollama is not None

    @property
    def is_llama_cpp(self) -> bool:
        return self.type == "llama_cpp" and self.llama_cpp is not None

    @property
    def is_tts_server(self) -> bool:
        return self.type == "tts_server" and self.tts is not None

    @property
    def is_asr_server(self) -> bool:
        return self.type == "asr_server" and self.asr is not None

    @property
    def is_ninfer(self) -> bool:
        return self.type == "ninfer" and self.ninfer is not None

    @property
    def health_url(self) -> Optional[str]:
        """Unified health URL accessor — reads backend-specific health_url or falls back to /health."""
        def _url(cfg, port_fallback: int) -> str:
            if hasattr(cfg, 'health_url'):
                return cfg.health_url or f"http://localhost:{port_fallback}/health"
            return f"http://localhost:{port_fallback}/health"
        if self.vllm:
            return _url(self.vllm, self.vllm.port)
        if self.sglang:
            return _url(self.sglang, self.sglang.port)
        if self.comfyui:
            return _url(self.comfyui, self.comfyui.port)
        if self.tts:
            return _url(self.tts, self.tts.port)
        if self.asr:
            return _url(self.asr, self.asr.port)
        if self.ollama_daemon:
            return _url(self.ollama_daemon, self.ollama_daemon.port)
        if self.ollama:
            return f"http://localhost:11434/api/tags"
        if self.llama_cpp:
            return _url(self.llama_cpp, self.llama_cpp.port)
        return f"http://localhost:{self.port}/health"

    @property
    def is_ollama_daemon(self) -> bool:
        return self.type == "ollama_daemon" and self.ollama_daemon is not None


# ─── Legacy Profile class (backward compat, will be removed in Phase 7) ──

# ─── Model Loading ───────────────────────────────────────────────

def load_models(models_dir: Path = MODELS_DIR, include_applied: bool = True) -> dict[str, ModelConfig]:
    """Load model configs from models.d/ directory.

    Each YAML file defines one model. The 'name' field must match the filename stem.
    include_applied=True（默认）额外 overlay 应用层 APPLIED_SCENARIOS_FILE
    （iff tune 写入的已应用场景；测试读纯 YAML 值时传 False）。
    Returns dict keyed by model name.
    """
    result: dict[str, ModelConfig] = {}
    # 规范化为 Path（接受 str/PathLike；非法输入 → 空结果，永不挂起——
    # MagicMock 等非真实路径的 exists()/glob() 会静默通过，但 PyYAML 读 mock 流会死循环）。
    models_dir = Path(models_dir)
    if not models_dir.exists():
        return result

    for yaml_file in sorted(models_dir.glob("*.yaml")):
        raw = yaml.safe_load(yaml_file.read_text())
        model_name = yaml_file.stem

        # Skip empty or invalid YAML files
        if raw is None:
            log.warning("Skipping empty YAML: %s", yaml_file.name)
            continue

        # Skip non-model YAML files (e.g. model_affinity.yaml)
        if not isinstance(raw, dict) or "name" not in raw:
            log.debug("Skipping non-model YAML: %s", yaml_file.name)
            continue

        # Validate name matches filename
        if raw.get("name") != model_name:
            raise ValueError(
                f"Name mismatch in {yaml_file}: YAML name='{raw.get('name')}' "
                f"vs filename stem='{model_name}'"
            )

        # Parse type
        model_type = raw.get("type", "vllm")

        # Parse vllm config if present
        vllm_cfg = None
        if raw.get("vllm"):
            vllm_raw = dict(raw["vllm"])
            # Extract sleep_mode sub-config before passing to VLLMConfig
            sleep_cfg = None
            if "sleep_mode" in vllm_raw:
                sleep_raw = vllm_raw.pop("sleep_mode")
                if sleep_raw and sleep_raw.get("enabled"):
                    sleep_cfg = SleepModeConfig(**sleep_raw)
            # Extract and validate extra_env before passing to VLLMConfig
            extra_env = vllm_raw.pop("extra_env", {}) or {}
            if not isinstance(extra_env, dict):
                log.warning("extra_env is not a dict in %s, ignoring", model_name)
                extra_env = {}
            for k in list(extra_env.keys()):
                if k in _PROTECTED_ENV_KEYS:
                    raise ConfigError(
                        f"extra_env key '{k}' is protected and cannot be overridden "
                        f"in model '{model_name}'"
                    )
                extra_env[k] = str(extra_env[k])
            vllm_cfg = VLLMConfig(**vllm_raw, extra_env=extra_env)
            vllm_cfg.sleep_mode = sleep_cfg
            # Parse startup_timeout from vllm section (overrides global)
            if "startup_timeout" in vllm_raw:
                vllm_cfg.startup_timeout = int(vllm_raw["startup_timeout"])

        # Parse sglang config if present
        sglang_cfg = None
        if raw.get("sglang"):
            sglang_raw = dict(raw["sglang"])
            extra_env = sglang_raw.pop("extra_env", {}) or {}
            if not isinstance(extra_env, dict):
                log.warning("extra_env is not a dict in %s, ignoring", model_name)
                extra_env = {}
            for k in list(extra_env.keys()):
                if k in _PROTECTED_ENV_KEYS:
                    raise ConfigError(
                        f"extra_env key '{k}' is protected and cannot be overridden "
                        f"in model '{model_name}'"
                    )
                extra_env[k] = str(extra_env[k])
            sglang_cfg = SGLangConfig(**sglang_raw, extra_env=extra_env)
            if "startup_timeout" in sglang_raw:
                sglang_cfg.startup_timeout = int(sglang_raw["startup_timeout"])

        # Parse comfyui config if present
        comfy_cfg = None
        if raw.get("comfyui"):
            comfy_cfg = ComfyUIConfig(**raw["comfyui"])

        # For type=comfyui, parse top-level comfyui fields
        if model_type == "comfyui" and not comfy_cfg:
            comfy_fields = {}
            for f in ("conda_env", "port", "working_dir", "health_url", "extra_flags"):
                if f in raw:
                    comfy_fields[f] = raw[f]
            if comfy_fields:
                comfy_cfg = ComfyUIConfig(**comfy_fields)

        # Parse ollama config if present
        ollama_cfg = None
        if raw.get("ollama"):
            ollama_cfg = OllamaModelConfig(**raw["ollama"])

        # Parse llama_cpp config if present
        llama_cpp_cfg = None
        if raw.get("llama_cpp"):
            llama_cpp_cfg = LlamaCppConfig(**raw["llama_cpp"])

        # Parse ollama_daemon config if present
        ollama_daemon_cfg = None
        if raw.get("ollama_daemon"):
            ollama_daemon_cfg = OllamaDaemonConfig(**raw["ollama_daemon"])

        # Parse tts_server config if present
        tts_cfg = None
        if raw.get("tts_server"):
            tts_raw = dict(raw["tts_server"])
            # Extract and validate extra_env (same protection as VLLMConfig)
            extra_env = tts_raw.pop("extra_env", {}) or {}
            if not isinstance(extra_env, dict):
                log.warning("extra_env is not a dict in %s, ignoring", model_name)
                extra_env = {}
            for k in list(extra_env.keys()):
                if k in _PROTECTED_ENV_KEYS:
                    raise ConfigError(
                        f"extra_env key '{k}' is protected and cannot be overridden "
                        f"in model '{model_name}'"
                    )
                extra_env[k] = str(extra_env[k])
            tts_cfg = TTSConfig(**tts_raw, extra_env=extra_env)

        # For type=tts_server, parse top-level tts_server fields
        if model_type == "tts_server" and not tts_cfg:
            tts_fields = {}
            for f in ("conda_env", "port", "working_dir", "health_url", "start_cmd"):
                if f in raw:
                    tts_fields[f] = raw[f]
            if tts_fields:
                tts_cfg = TTSConfig(**tts_fields)

        # Parse asr_server config if present
        asr_cfg = None
        if raw.get("asr_server"):
            asr_raw = dict(raw["asr_server"])
            extra_env = asr_raw.pop("extra_env", {}) or {}
            if not isinstance(extra_env, dict):
                log.warning("extra_env is not a dict in %s, ignoring", model_name)
                extra_env = {}
            for k in list(extra_env.keys()):
                if k in _PROTECTED_ENV_KEYS:
                    raise ConfigError(
                        f"extra_env key '{k}' is protected and cannot be overridden "
                        f"in model '{model_name}'"
                    )
                extra_env[k] = str(extra_env[k])
            asr_cfg = ASRConfig(**asr_raw, extra_env=extra_env)

        # For type=asr_server, parse top-level asr_server fields
        if model_type == "asr_server" and not asr_cfg:
            asr_fields = {}
            for f in ("conda_env", "port", "working_dir", "health_url", "start_cmd"):
                if f in raw:
                    asr_fields[f] = raw[f]
            if asr_fields:
                asr_cfg = ASRConfig(**asr_fields)

        # For type=ollama, parse top-level ollama fields
        if model_type == "ollama" and not ollama_cfg:
            ollama_fields = {}
            for f in ("model_ref", "keep_alive"):
                if f in raw:
                    ollama_fields[f] = raw[f]
            if ollama_fields:
                ollama_cfg = OllamaModelConfig(**ollama_fields)

        # For type=llama_cpp, parse top-level llama_cpp fields
        if model_type == "llama_cpp" and not llama_cpp_cfg:
            cpp_fields = {}
            for f in ("model_path", "port", "threads", "context_size", "gpu_layers"):
                if f in raw:
                    cpp_fields[f] = raw[f]
            if cpp_fields:
                llama_cpp_cfg = LlamaCppConfig(**cpp_fields)

        # For type=ollama_daemon, parse top-level ollama_daemon fields
        if model_type == "ollama_daemon" and not ollama_daemon_cfg:
            daemon_fields = {}
            for f in ("port", "health_url", "data_dir"):
                if f in raw:
                    daemon_fields[f] = raw[f]
            if daemon_fields:
                ollama_daemon_cfg = OllamaDaemonConfig(**daemon_fields)

        # Parse ninfer config if present
        ninfer_cfg = None
        if raw.get("ninfer"):
            ninfer_cfg = NInferConfig(**raw["ninfer"])

        # For type=ninfer, parse top-level ninfer fields
        if model_type == "ninfer" and not ninfer_cfg:
            ninfer_fields = {}
            for f in ("weight_path", "docker_image", "port", "max_context", "kv_dtype"):
                if f in raw:
                    ninfer_fields[f] = raw[f]
            if ninfer_fields:
                ninfer_cfg = NInferConfig(**ninfer_fields)

        # Backward compat: YAML 'mode' → gpu_role
        mode_val = raw.get("mode", raw.get("gpu_role", "none"))
        result[model_name] = ModelConfig(
            name=model_name,
            description=raw.get("description", model_name),
            gpu_role=mode_val,
            type=model_type,
            vllm=vllm_cfg,
            sglang=sglang_cfg,
            comfyui=comfy_cfg,
            ollama=ollama_cfg,
            llama_cpp=llama_cpp_cfg,
            ollama_daemon=ollama_daemon_cfg,
            tts=tts_cfg,
            asr=asr_cfg,
            ninfer=ninfer_cfg,
            typical_vram_pct=float(raw.get("typical_vram_pct", 0)),
            peak_vram_mb=int(raw.get("peak_vram_mb", 0)),
            model_type=raw.get("model_type", "llm"),
            quantization=raw.get("quantization", ""),
            modality=raw.get("modality", ""),
            replicas=raw.get("replicas", []),
            deployment=raw.get("deployment", ""),
        )
        # 场景预设（iff tune）: 顶层 presets（内联兜底）+ 源 YAML 路径。
        # active_preset 不从 YAML 读——应用层（APPLIED_SCENARIOS_FILE）是单一来源。
        result[model_name].presets = dict(raw.get("presets") or {})
        result[model_name].yaml_path = str(yaml_file)

    # 场景预设侧车文件 models.d/scenarios.yaml（手动配置的单一真相源）：
    # 顶层键 = 模型名；场景字典合并进 model.presets（同名覆盖模型 YAML 内联）。
    # 物理 KV 池上限不是侧车字段——它就是各模型 YAML 引擎块里的固定 kv_capacity。
    _merge_scenarios(models_dir, result)

    if include_applied:
        _merge_applied_layers(result)

    return result


def read_applied_scenarios() -> dict:
    """只读应用层文件（D5：展示层/合并共用的磁盘真相源）。

    返回 {model_name: {active_preset, overrides}}；文件缺失/损坏 → {}（静默回退）。
    只由 tune 写入（带文件锁），这里永不写。
    """
    path = APPLIED_SCENARIOS_FILE
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        log.warning("读取应用层 %s 失败: %s", path, e)
        return {}
    return raw if isinstance(raw, dict) else {}


def current_active_preset(model_name: str) -> str:
    """某模型当前生效场景（D5：直读应用层文件，≡ 容器实际值）。

    空条目/无条目 = "default"（default 一等化：default = 模型 YAML 当前值）。
    """
    entry = read_applied_scenarios().get(model_name) or {}
    return str(entry.get("active_preset") or "default")


def _merge_applied_layers(models: dict[str, "ModelConfig"]) -> None:
    """应用层 overlay：APPLIED_SCENARIOS_FILE（~/.inferfabric/active_scenarios.yaml）。

    机器本地状态，只由 tune 写入，其余工具链只读。格式:
        {model_name: {active_preset: "short-ctx", overrides: {字段: 值}}}
    overrides 只设置引擎配置块上已存在的字段（未知字段忽略，防坏数据污染）。
    文件缺失/损坏 → 静默跳过（回退纯 YAML 值）。
    """
    raw = read_applied_scenarios()
    for mname, data in raw.items():
        model = models.get(mname)
        if model is None or not isinstance(data, dict):
            continue
        model.active_preset = str(data.get("active_preset") or "")
        overrides = data.get("overrides") or {}
        cfg = model.engine_config
        if not isinstance(overrides, dict) or cfg is None:
            continue
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)


def _merge_scenarios(models_dir: Path, models: dict[str, "ModelConfig"]) -> None:
    scn_path = models_dir / "scenarios.yaml"
    if not scn_path.exists():
        return
    try:
        scn = yaml.safe_load(scn_path.read_text()) or {}
    except Exception as e:
        log.warning("读取场景预设 %s 失败: %s", scn_path, e)
        return
    if not isinstance(scn, dict):
        log.warning("场景预设 %s 顶层不是映射，忽略", scn_path)
        return
    for mname, data in scn.items():
        model = models.get(mname)
        if model is None:
            log.warning("scenarios.yaml 引用未知模型 %s，忽略", mname)
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if isinstance(v, dict):
                model.presets[k] = dict(v)  # 侧车场景覆盖模型 YAML 内联
            else:
                log.debug("scenarios.yaml %s 的 %s 非场景映射，忽略", mname, k)


# ─── Retry Constants (CCR-style) ─────────────────────────────────

UPSTREAM_RETRY_BASE_S = 0.5
UPSTREAM_RETRY_MAX_S = 2.0
UPSTREAM_LOCAL_RETRIES = 2  # 1 attempt + 2 retries = 3 total local attempts


def exponential_backoff(attempt: int) -> float:
    """CCR-style exponential backoff: base * 2^attempt, clamped to max."""
    return min(UPSTREAM_RETRY_MAX_S, UPSTREAM_RETRY_BASE_S * (2 ** attempt))


def should_retry_on_status(status: int) -> bool:
    """Should we retry on this HTTP status? CCR-style decision.

    - 5xx / 408 / 429 → retry with backoff
    - 4xx (non-retryable) → skip retry, fall back immediately
    - 2xx / 3xx → success, no retry needed
    """
    if status >= 500 or status in (408, 429):
        return True
    return False


def parse_retry_after_ms(headers: dict) -> float | None:
    """Parse retry-after header (seconds or date), CCR-style.
    Returns milliseconds to wait, or None."""
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
        return max(0, min(secs * 1000, 60_000))  # cap at 60s like CCR
    except ValueError as e:
        log.debug("Config parse warning: %s", e)
    return None


# ─── Model Affinity (static routing) ──────────────────────────────

def load_model_affinity(models_dir: Path = MODELS_DIR) -> dict[str, str]:
    """Load model_affinity.yaml → {model_name_pattern: routing_target}.

    Cached with mtime invalidation — called on every request (hot path).
    Thread-safe: uses module-level lock for cache reads/writes.

    Example YAML:
      baidu:
        - "deepseek-v4-flash"
        - "glm-5"

    Returns: {"deepseek-v4-flash": "baidu", "glm-5": "baidu", ...}
    """
    affinity_file = models_dir / "model_affinity.yaml"
    if not affinity_file.exists():
        return {}

    # Cache with mtime invalidation (thread-safe)
    try:
        current_mtime = affinity_file.stat().st_mtime
    except OSError:
        return {}

    with _affinity_lock:
        if load_model_affinity._cache is not None and load_model_affinity._mtime == current_mtime:
            return load_model_affinity._cache

    # I/O outside lock
    raw = yaml.safe_load(affinity_file.read_text())
    if not raw or not isinstance(raw, dict):
        result = {}
    else:
        result = {}
        for target, patterns in raw.items():
            if isinstance(patterns, list):
                for p in patterns:
                    result[p] = target

    with _affinity_lock:
        # Double-check: another thread may have populated cache during I/O
        if load_model_affinity._cache is not None and load_model_affinity._mtime == current_mtime:
            return load_model_affinity._cache
        load_model_affinity._cache = result
        load_model_affinity._mtime = current_mtime
    return result


_affinity_lock = threading.Lock()
load_model_affinity._cache = None
load_model_affinity._mtime = 0.0
