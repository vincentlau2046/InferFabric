"""能力包 host facts 探测 — 阶段 1 Task 1（specs/packs-platform-adaptation §3）。

纪律（与 platform.probe 一致）：探测失败返回保守值，绝不抛异常；
命令在 PATH ≠ 设备可用；探测超时/输出异常 = `unknown`（不是 `none`，
后者仅指设备/驱动确定不存在）。本模块只产出事实，不做准入判断
（模板求交在 packs.admission，Task 2）。
"""
from __future__ import annotations

import platform as _pyplatform
import subprocess
from dataclasses import dataclass
from typing import Callable, Literal

from . import detect

__all__ = ["PackHostFacts", "probe_pack_host", "platform_key"]

Accelerator = Literal["nvidia", "none", "unknown"]

# 有界探测：nvidia-smi 单字段查询，5s 超时
_NVIDIA_QUERY = ["nvidia-smi", "--query-gpu=compute_cap",
                 "--format=csv,noheader,nounits"]
_NVIDIA_TIMEOUT_S = 5

_ARCH_ALIASES = {
    "aarch64": "arm64",
    "arm64": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}


@dataclass(frozen=True)
class PackHostFacts:
    """本机平台事实（探测产出，非声明）。"""

    os: str  # "darwin" | "linux" | "other"（来自 platform.detect）
    arch: str  # 归一化："arm64" | "x86_64" | 原样
    accelerator: Accelerator
    compute_capability: tuple[int, int] | None  # 仅 accelerator == "nvidia"


def _normalize_arch(raw: str) -> str:
    return _ARCH_ALIASES.get(raw.strip().lower(), raw.strip().lower())


def _probe_nvidia(run: Callable) -> tuple[Accelerator,
                                          tuple[int, int] | None]:
    """有界 nvidia-smi 探测。

    - 命令缺失（FileNotFoundError）→ ("none", None)：驱动确定不存在。
    - 超时 / 非零退出 / 输出不可解析 → ("unknown", None)：fail-closed。
    - 成功且可解析 → ("nvidia", (maj, min))。
    """
    try:
        r = run(_NVIDIA_QUERY, capture_output=True, text=True,
                timeout=_NVIDIA_TIMEOUT_S)
    except FileNotFoundError:
        return "none", None
    except (subprocess.TimeoutExpired, OSError):
        return "unknown", None
    if getattr(r, "returncode", 1) != 0:
        return "unknown", None
    out = (getattr(r, "stdout", "") or "").strip().splitlines()
    if not out:
        return "unknown", None
    first = out[0].strip().lower()
    if first in ("", "n/a", "[n/a]", "not supported"):
        return "unknown", None
    parts = first.replace(" ", "").split(".")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return "nvidia", (int(parts[0]), int(parts[1]))
    return "unknown", None


def probe_pack_host(*, os_name: str | None = None,
                    arch: str | None = None,
                    run: Callable | None = None) -> PackHostFacts:
    """探测本机平台事实；注入参数仅供测试，生产调用无参。

    run 注入替代 subprocess.run（测试断言超时/缺失/垃圾输出分支）。
    """
    os_resolved = os_name if os_name is not None else detect().os
    arch_resolved = _normalize_arch(arch if arch is not None
                                    else _pyplatform.machine())
    if run is None:
        run = subprocess.run

    if os_resolved != "linux":
        # macOS / other：无 NVIDIA CUDA 遥测路径，设备确定为无 → none
        return PackHostFacts(os=os_resolved, arch=arch_resolved,
                             accelerator="none", compute_capability=None)
    accelerator, cc = _probe_nvidia(run)
    return PackHostFacts(os=os_resolved, arch=arch_resolved,
                         accelerator=accelerator, compute_capability=cc)


def platform_key(facts: PackHostFacts) -> str:
    """平台事实 → 平台键（与 schema.PLATFORM_KEYS 同一词汇）。

    darwin-x86 / unknown 不在白名单内：除显式多平台申明外不可匹配；
    归类和准入语义由 packs.admission 决定。
    """
    if facts.os == "darwin":
        return "darwin-arm" if facts.arch == "arm64" else "darwin-x86"
    if facts.os == "linux":
        if facts.accelerator == "nvidia":
            return "linux-nvidia"
        if facts.accelerator == "none":
            return "linux-cpu"
        return "linux-unknown"
    return "unknown"
