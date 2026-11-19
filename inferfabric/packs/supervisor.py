"""PackSupervisor — 进程组隔离与 drain — PK2（Task 9）。

spec §8.1 底线：包引擎进程独立进程组（start_new_session/os.setsid），退出/
超时/卸载时整组收割；spec §7.2 裁决：卸载 drain 上限 10s，超时对进程组
SIGKILL，在途请求按 503 返回（503 语义在 uninstaller/handler 层，本模块
只负责进程事实）。

资源约束现实（spec §8.4）：POSIX rlimit 管 CPU/文件描述符/进程数；macOS
内存 rlimit 不可靠（no-op），内存控制走安装/启动准入 + 现有内存账本，
RSS watchdog 随 SBPL 立项交付。
"""

import os
import resource
import signal
import time
from collections import namedtuple
from dataclasses import dataclass

DRAIN_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.1

RLIMIT_NOFILE_LIMIT = 256
RLIMIT_NPROC_LIMIT = 64
RLIMIT_CPU_SECONDS = 3600

DrainResult = namedtuple("DrainResult", "stopped forced_kill elapsed_s")


def pack_rlimits(manifest) -> dict[int, int]:
    """spawn 前置于子进程的 POSIX rlimit（经 preexec_fn 应用）。

    常量取自 resource 模块（uv standalone 构建的 os 模块不导出 RLIMIT_*）。
    不含 RLIMIT_AS/RLIMIT_RSS — macOS 上为 no-op，不得冒充内存硬上限。
    """
    return {
        resource.RLIMIT_NOFILE: RLIMIT_NOFILE_LIMIT,
        resource.RLIMIT_NPROC: RLIMIT_NPROC_LIMIT,
        resource.RLIMIT_CPU: RLIMIT_CPU_SECONDS,
    }


@dataclass
class PackSupervisor:
    drain_timeout_s: float = DRAIN_TIMEOUT_S
    poll_interval_s: float = POLL_INTERVAL_S
    store: object | None = None    # PackStore，注入以支持崩溃标记

    def drain_and_stop(self, name: str, stop_fn, is_alive_fn,
                       pgid_fn=None) -> DrainResult:
        """停包进程组：stop_fn 发起停止，轮询 is_alive_fn 至超时强杀。

        pgid_fn 返回包进程组 ID（由 spawn 侧 start_new_session 建立，
        pgid == 子 pid）。pgid_fn 缺失时**绝不盲杀** — 只报 stopped=False，
        强杀留给持有 PGID 的调用方（防误杀网关自身进程组）。
        """
        start = time.monotonic()
        stop_fn()
        while time.monotonic() - start < self.drain_timeout_s:
            if not is_alive_fn():
                return DrainResult(stopped=True, forced_kill=False,
                                   elapsed_s=time.monotonic() - start)
            time.sleep(self.poll_interval_s)
        elapsed = time.monotonic() - start
        if pgid_fn is None:
            return DrainResult(stopped=False, forced_kill=False,
                               elapsed_s=elapsed)
        pgid = pgid_fn()
        if pgid is None:
            # pgid_fn 动态返回 None（进程已无组可查）→ 同"无 PGID 不盲杀"
            return DrainResult(stopped=False, forced_kill=False,
                               elapsed_s=elapsed)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # 组已不存在 → 视为终止
        except OSError:
            # EPERM 等：强杀未生效，不得谎报 stopped（终审 deferred #1）
            return DrainResult(stopped=False, forced_kill=True,
                               elapsed_s=elapsed)
        return DrainResult(stopped=True, forced_kill=True, elapsed_s=elapsed)

    def mark_crashed(self, pack_id: str, reason: str) -> None:
        """包进程异常退出：标 degraded，只动该包，不污染其他包（spec §7.3）。"""
        if self.store is None:
            return
        rec = self.store.get(pack_id)
        if rec is None:
            return
        self.store.update(pack_id, status="degraded",
                          error=f"crashed:{reason}")
