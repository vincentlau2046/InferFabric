"""Platform probes — ports, unified-memory watermark, inference budget.

Discipline: every probe returns a conservative value on failure, never raises.

Budget B = RAM − reserve (the single formula). Precedence:
IFF_RESERVE_MB env > packs policy.json system_reserved_memory_mb
> default 8192. Core computation lives in `_budget_core`; `budget_mb()`
resolves `mem_total_mb` through this module's globals at call time so
tests can patch it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import detect

log_label = "inferfabric.platform"

IS_MACOS = detect().os == "darwin"

DEFAULT_RESERVE_MB = 8 * 1024  # 8192MB


def _policy_reserve_mb():
    """读设置页（packs policy）的 system_reserved_memory_mb。

    直接读 JSON 不 import packs（避免 packs→config→本模块 环）；
    缺失/损坏/非法一律 None 回退默认，绝不抛异常（本模块纪律）。
    """
    state_dir = os.environ.get("IFF_DATA_DIR") or os.environ.get(
        "MACFABRIC_STATE_DIR",
        os.path.join(os.path.expanduser("~"), ".inferfabric"))
    path = os.path.join(state_dir, "packs", "policy.json")
    try:
        with open(path, encoding="utf-8") as f:
            mb = int(json.load(f)["system_reserved_memory_mb"])
        return mb if mb >= 1 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ─── 端口 → PID ──────────────────────────────────────────────────

def pid_on_port(port: int) -> int | None:
    """返回占用 TCP 监听端口的 PID；探测失败（端口无人监听）返回 None。

    macOS: lsof -ti tcp:PORT -sTCP:LISTEN（fuser 不存在于 macOS）
    Linux: fuser PORT/tcp — 契约与上游 v1 _port_pid 一致（评审 PR#2-3）：
    timeout=5；TimeoutExpired / FileNotFoundError（fuser 缺失）向上传播
    fail-visible，不吞异常（否则 GPU 态推导会把在跑引擎误报 idle）。
    """
    if IS_MACOS:
        r = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip().splitlines()[0])
        return None
    r = _run(["fuser", str(port) + "/tcp"], timeout=5)
    if r.returncode == 0:
        m = re.search(r"(\d+)", r.stdout)
        return int(m.group(1)) if m else None
    return None


# ─── 统一内存水位 ────────────────────────────────────────────────

def mem_total_mb() -> int | None:
    """物理内存总量 MB。macOS 用 sysctl hw.memsize；非 macOS 返回 None。"""
    if not IS_MACOS:
        return None
    try:
        r = _run(["sysctl", "-n", "hw.memsize"])
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return int(r.stdout.strip()) // (1024 * 1024)
    except Exception:
        pass
    return None


def mem_used_mb() -> int | None:
    """已用内存 MB = total − (free + inactive + purgeable)。

    vm_stat 口径：free/inactive/speculative 页可回收，active/wired/compressed
    视为在用。失败返回 None。
    """
    return mem_used_given_total(mem_total_mb())


def mem_used_given_total(total_mb: int | None) -> int | None:
    """vm_stat 已用水位，接受外部采样好的物理总量（Apple 遥测适配器用：

    used 与 budget 共享同一次 hw.memsize 读取，避免同帧两次 sysctl 得到
    不一致的窗口）。total 未知 → None。"""
    if total_mb is None:
        return None
    if not IS_MACOS:
        return None
    try:
        r = _run(["vm_stat"])
        if r.returncode != 0:
            return None
        pages = {}
        page_size = 4096
        m = re.search(r"page size of (\d+) bytes", r.stdout)
        if m:
            page_size = int(m.group(1))
        for line in r.stdout.splitlines():
            mm = re.match(r"^(.+):\s+(\d+)", line.strip())
            if mm:
                pages[mm.group(1).rstrip(":")] = int(mm.group(2))

        def pg(key: str) -> int:
            for k, v in pages.items():
                if k.startswith(key):
                    return v
            return 0

        reclaimable = (pg("Pages free") + pg("Pages inactive")
                       + pg("Pages speculative"))
        used_mb = total_mb - (reclaimable * page_size) // (1024 * 1024)
        return max(used_mb, 0)
    except Exception:
        return None


def memory_pressure_level() -> int | None:
    """macOS 内核内存压力档位：1=normal 2=warning 3=critical。

    sysctl kern.memorystatus_vm_pressure_level（非特权可读）。
    只认 1..3；非 macOS / 命令失败 / 未知值 → None（观测 fail-open）。
    """
    if not IS_MACOS:
        return None
    try:
        r = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
        if r.returncode == 0:
            v = r.stdout.strip()
            if v.isdigit() and int(v) in (1, 2, 3):
                return int(v)
    except Exception:
        pass
    return None


# ─── 预算 B（内存账本地基）─────────────────────────────────────

def _reserve_mb() -> int:
    """保留值优先级：env > packs policy > 默认 8192。

    env 双读：IFF_RESERVE_MB（上游名，PR#2 起两边文本一致）优先，
    MACFABRIC_RESERVE_MB 为 fork 兼容别名。
    """
    env = (os.environ.get("IFF_RESERVE_MB")
           or os.environ.get("MACFABRIC_RESERVE_MB", "")).strip()
    reserve = None
    if env:
        try:
            reserve = int(env)
        except ValueError:
            reserve = None
    if reserve is None:
        reserve = _policy_reserve_mb() or DEFAULT_RESERVE_MB
    return reserve


def _budget_core(total_mb: int | None) -> int:
    """B = total − reserve（唯一公式，无第二道上限）。total 未知 → 0。"""
    if total_mb is None:
        return 0
    return max(total_mb - _reserve_mb(), 0)


def budget_mb() -> int:
    """统一内存推理预算 B = RAM − 保留。

    每次调用重读 policy —— 设置页保存后即时生效（账本准入每次动态重建，
    无缓存层）。非 macOS 返回 0（调用方以 IS_MACOS 门禁在前）。
    """
    return _budget_core(mem_total_mb())


def budget_given_total(total_mb: int | None) -> int | None:
    """观测语义的预算：total 未知 → None（不发布账本内部的 0 兜底）。

    Apple 遥测适配器发布快照用；准入仍走 budget_mb()（保守 0 语义）。
    """
    if total_mb is None:
        return None
    return _budget_core(total_mb)
