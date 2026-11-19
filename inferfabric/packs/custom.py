"""用户扩展能力包 — 统一架构裁决（2026-11-16，用户原话见 spec）。

用户只提交 下载链接 + 名称 + 能力类型（首期限 llama.cpp 框架内三能力），
网关校验链任一不过即失败且报错明确：
  https URL → 下载（HttpArtifactFetcher，Range 断点续传）→
  sha256（可选，提供即校验）→ GGUF 魔数（格式）→ 磁盘余量门禁 →
  真引擎隔离试加载（framework 可运行性的最终裁决）→ 注册 custom.<name>。

设计取舍：自定义包构造**真 Manifest**（parse_manifest 严格校验），
catalog 合并、卸载、失败重试、状态机全部复用官方包既有链路——
不做平行体系（"一定是统一的"）。
"""

import hashlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from inferfabric.packs.schema import parse_manifest

GGUF_MAGIC = b"GGUF"
CUSTOM_CAPABILITIES = ("llm", "vl", "embedding")
CUSTOM_VERSION = "1.0.0"
CUSTOM_TEMPLATE = "llama_cpp_chat_v1"      # App templateReady 镜像的模板名
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_gguf(path) -> None:
    """GGUF 魔数校验；不符抛 ValueError("not_gguf_format")。"""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic != GGUF_MAGIC:
        raise ValueError(
            f"not_gguf_format: {Path(path).name} 不是 GGUF 模型文件"
            f"（magic={magic!r}）— 仅支持 llama.cpp 框架（GGUF）")


def validate_custom_body(body) -> tuple[str, str, str, str | None]:
    """校验自定义安装请求 → (name, url, capability, sha256|None)。

    任一违规抛 ValueError（消息面向用户可直接展示）。
    """
    if not isinstance(body, dict):
        raise ValueError("body 必须是 JSON 对象")
    name = str(body.get("name") or "")
    url = str(body.get("url") or "")
    capability = str(body.get("capability") or "")
    sha256 = body.get("sha256")
    if not NAME_RE.match(name):
        raise ValueError("name 需为 3-40 位小写字母/数字/连字符")
    if not url.startswith("https://"):
        raise ValueError("url 必须是 https（拒绝明文下载源）")
    if capability not in CUSTOM_CAPABILITIES:
        raise ValueError(
            f"capability 仅支持 {'/'.join(CUSTOM_CAPABILITIES)}"
            f"（llama.cpp 框架）；tts/asr/ocr 运行时未接入")
    if sha256 is not None:
        sha256 = str(sha256).lower()
        if not _SHA_RE.match(sha256):
            raise ValueError("sha256 需为 64 位十六进制（可选，提供即校验）")
    return name, url, capability, sha256


def build_custom_manifest(name: str, capability: str, url: str,
                          sha256: str | None, size_mb: int):
    """由用户输入构造真 Manifest（parse_manifest 严格校验把关）。

    pack_id 用 capability 前缀（schema 硬约束：前缀==capability）——
    自定义包与官方包同一命名空间，与官方重名自然被 409 挡下。
    """
    size_mb = max(int(size_mb), 1)
    # 参数量未知 → 按体量保守估算（≈0.5 B/param，4bit 量化典型密度）；
    # 估算偏大偏保守 — 只影响 policy 参数门禁，不影响运行
    param_b = max(1, round(size_mb * 1024 * 1024 / (0.5 * 1024 ** 3)))
    d = {
        "pack_id": f"{capability}.{name}",
        "pack_version": CUSTOM_VERSION,
        "capability": capability,
        "model": {"id": name, "parameter_count_b": param_b,
                  "quantization": "gguf", "license": "user-provided"},
        "runtime": {"engine": "llama_cpp",
                    "runtime_template": "llama-cpp-macos-arm64-v1"},
        "resources": {"download_mb": size_mb,
                      "disk_mb": size_mb + 64,
                      # 权重 + 上下文/KV 开销粗估；真门禁在引擎试加载
                      "peak_memory_mb": int(size_mb * 1.2) + 256,
                      "gpu_role": "shared"},
        "lifecycle": {"mode": "on_demand"},
        "network": {"listen": "loopback", "allowed_upstream": []},
        "artifacts": [
            {"id": "model", "kind": "model", "source": url,
             "sha256": sha256 or "0" * 64},
            # 引擎为复用（runtime 符号链接到已装包）——id 必须为 engine，
            # config_gen 由此推出 binary_path = pack_dir/runtime/engine
            {"id": "engine", "kind": "runtime",
             "source": "reused://llama-cpp-engine", "sha256": "0" * 64},
        ],
        "execution": {"model_config_template": CUSTOM_TEMPLATE,
                      "healthcheck": "openai_chat_ready_v1"},
        "sandbox": {"policy": "verified-local-pack-v1",
                    "degraded_install_allowed": True},
    }
    return parse_manifest(d)


@dataclass
class SimpleArtifact:
    """fetcher 契约最小实现（HttpArtifactFetcher 只读 .source）。"""
    id: str
    kind: str
    source: str
    sha256: str = ""


class CustomPackRegistry:
    """user_custom.json 持久化：pack_id → {name,url,sha256,capability,manifest}。"""

    def __init__(self, path):
        self._path = Path(path)

    def _load(self) -> dict:
        try:
            import json
            with open(self._path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        import json
        import os
        self._path.parent.mkdir(parents=True, exist_ok=True)
        part = f"{self._path}.part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(part, self._path)

    def get(self, pack_id: str) -> dict | None:
        return self._load().get(pack_id)

    def all(self) -> dict:
        return self._load()

    def add(self, entry: dict) -> None:
        d = self._load()
        d[entry["pack_id"]] = entry
        self._save(d)

    def remove(self, pack_id: str) -> None:
        d = self._load()
        if d.pop(pack_id, None) is not None:
            self._save(d)


def find_reusable_engine(root: Path):
    """已装 llama.cpp 包的 runtime/engine（自定义包无引擎 artifact，复用之）。

    拒绝 gzip 残留（runtime 未准备的历史故障模式）；找不到返回 None。
    """
    for eng in sorted(Path(root).glob("*/*/runtime/engine")):
        try:
            if eng.is_file() and not eng.is_symlink() \
                    and bool(eng.stat().st_mode & 0o111):
                with open(eng, "rb") as f:
                    if f.read(2) != b"\x1f\x8b":
                        return eng
        except OSError:
            continue
    return None


def now_ts() -> int:
    return int(time.time())
