"""部署向导后端 — 能力×引擎×平台收窄（GET /admin/deploy/options）。

收窄在 Python 侧完成且不可绕过：向导内不出被收窄掉的选项。
- darwin/arm64 → llama_cpp（Metal 全卸载，2026-11 裁决）
- linux + nvidia-smi → vllm/sglang/llama_cpp
- 其余（含 nvidia-smi 探测失败）→ llama_cpp（Review Focus 2：探测失真
  按「无 NVIDIA」收窄，绝不出 vllm 选项）

引擎可用性仅探测不安装（packs 体系只作探测与 deep-link，不扩范围）：
llama_cpp 查 packs 可复用引擎或 PATH 上的 llama-server；vllm/sglang 查
PATH 二进制。
"""
import platform
import shutil

ENGINE_LABELS = {
    "llama_cpp": "llama.cpp",
    "vllm": "vLLM",
    "sglang": "SGLang",
}
CAPABILITIES = ["llm", "embedding", "tts", "asr", "ocr"]
# 部署方式沿用引擎既有的装载通道：llama_cpp 走本地 GGUF 或 packs 目录，
# vllm/sglang 只支持本地目录（权重经下载器落盘后由 local-dir 装载）
DEPLOYMENT_MODES = {
    "llama_cpp": ["local-gguf", "packs-catalog"],
    "vllm": ["local-dir"],
    "sglang": ["local-dir"],
}


def _default_engine_available(engine_id: str) -> bool:
    """真实探测（生产默认；测试经 engine_available 注入）。"""
    if engine_id == "llama_cpp":
        try:
            from inferfabric.packs.custom import find_reusable_engine
            from inferfabric.proxy.pack_handlers import get_pack_manager
            if find_reusable_engine(get_pack_manager().root) is not None:
                return True
        except Exception:
            pass
        return shutil.which("llama-server") is not None
    return shutil.which(engine_id) is not None


def narrowed_engines(os_name: str, has_nvidia: bool | None,
                     engine_available=None) -> list[dict]:
    """按平台收窄引擎清单；has_nvidia=None（探测失败）按无 NVIDIA。"""
    if engine_available is None:
        engine_available = _default_engine_available
    if os_name == "darwin":
        ids = ["llama_cpp"]
    elif has_nvidia is True:
        ids = ["vllm", "sglang", "llama_cpp"]
    else:
        ids = ["llama_cpp"]
    return [{"id": eid, "label": ENGINE_LABELS[eid],
             "available": bool(engine_available(eid))} for eid in ids]


def deploy_options(os_name: str | None = None, arch: str | None = None,
                   has_nvidia: bool | None = None,
                   engine_available=None) -> dict:
    """向导首屏选项（纯函数；默认从真实平台探测，参数注入供测试）。"""
    if os_name is None:
        os_name = platform.system().lower()
    if arch is None:
        arch = platform.machine().lower()
    if has_nvidia is None:
        has_nvidia = shutil.which("nvidia-smi") is not None
    return {
        "capabilities": list(CAPABILITIES),
        "engines": narrowed_engines(os_name, has_nvidia, engine_available),
        "platform": {"os": os_name, "arch": arch, "nvidia": has_nvidia},
        "deployment_modes": {k: list(v) for k, v in DEPLOYMENT_MODES.items()},
    }
