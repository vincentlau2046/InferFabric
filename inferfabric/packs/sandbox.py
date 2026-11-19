"""沙箱能力探测与 Provider 接口 — PK0（Task 3）。

spec §8.2：
- 产品优先路径 = SBPL profile API 注入；sandbox-exec 仅开发/兼容诊断，不作为
  产品前提；探测枚举 full / degraded / unavailable（PK0 冻结）。
- 探测失败（UNAVAILABLE）按 DEGRADED 处理并保留诊断 — 准入判定在 installer。
- degraded 兜底 = 进程组 + 目录隔离 + 资源监控 + 网络约束能做到的部分，
  绝不把降级状态冒充完整隔离（spec §8.3）。

SBPL API 注入的真实实现按 PK3 后实测立项；本模块先冻结接口 + Noop 兜底。
"""

import ctypes
import ctypes.util
import platform
from dataclasses import dataclass
from enum import Enum


class SandboxMode(Enum):
    FULL = "full"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SandboxProbe:
    mode: SandboxMode
    detail: str


def _detect_mac_version() -> tuple[int, int] | None:
    try:
        major, minor = platform.mac_ver()[0].split(".")[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def _detect_sbpl_api() -> bool | None:
    """ctypes 探测 libc 的 sandbox_init 符号（SBPL C API 入口）。

    返回 None = 探测自身失败（库加载异常等），与 False（符号不存在）区分。
    """
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib")
        return hasattr(lib, "sandbox_init")
    except OSError:
        return None


def probe_sandbox(mac_version: tuple[int, int] | None = None,
                  has_sbpl_api: bool | None = None) -> SandboxProbe:
    """探测本机沙箱能力。两参数均 None 时自检；注入参数供测试与降级诊断。"""
    if mac_version is None:
        mac_version = _detect_mac_version()
    if has_sbpl_api is None:
        has_sbpl_api = _detect_sbpl_api()

    if mac_version == (0, 0) or (mac_version is None and has_sbpl_api is None):
        return SandboxProbe(
            SandboxMode.UNAVAILABLE,
            "sandbox capability probe failed (no macOS version / libc)")

    if mac_version is None or has_sbpl_api is None:
        return SandboxProbe(
            SandboxMode.UNAVAILABLE,
            f"partial probe failure: mac_version={mac_version}, "
            f"has_sbpl_api={has_sbpl_api}")

    if has_sbpl_api:
        return SandboxProbe(
            SandboxMode.FULL,
            f"SBPL C API available (macOS {mac_version[0]}.{mac_version[1]})")
    return SandboxProbe(
        SandboxMode.DEGRADED,
        f"SBPL C API unavailable on macOS {mac_version[0]}.{mac_version[1]}; "
        "process-group/dir/network constraints only")


class SandboxProvider:
    """包进程隔离属性提供方。apply 返回加进引擎启动的属性/env。"""

    def apply(self, profile_name: str, pack_dir, env: dict) -> dict:  # pragma: no cover
        raise NotImplementedError


class NoopProvider(SandboxProvider):
    """degraded 兜底：独立进程组 + 包目录约束 + 网关 loopback 调用。

    显式 sandbox_mode=degraded — 不冒充完整隔离（spec §8.3）。
    """

    def apply(self, profile_name: str, pack_dir, env: dict) -> dict:
        return {
            "start_new_session": True,   # os.setsid → killpg 整组收割
            "sandbox_mode": "degraded",
            "sandbox_profile": profile_name,
            "env": dict(env),
        }
