"""父进程消亡自退（issue #50）— 验收实例孤儿进程防护。

背景：验收/临时实例（隔离 HOME + 独立端口）的网关在启动它的脚本/会话
被 SIGKILL 或异常退出后，会作为孤儿长期存活（2026-11-25 实锤：11-20 的
验收网关在 job 结束后存活 5 天，占用 8990 并与生产 8999 形成双面板）。

机制：macOS 无 prctl(PDEATHSIG)，采用轮询 `getppid()` —— 父进程死后子
进程被 reparent（ppid 变化，通常 → 1/launchd），守护线程检测到变化即
`os._exit(0)`。

启用边界：**仅显式 opt-in**（`IFF_PARENT_WATCH=1`）。生产 launchd 网关
不设该 env，`start_parent_watch()` 直接返回 None，行为不受影响；验收
启动脚本 `scripts/iff-acceptance.sh` 统一设置。

线程为 daemon：不阻塞正常退出路径；正常 SIGTERM 仍走既有信号处理。
"""

import logging
import os
import threading
import time

ENV_VAR = "IFF_PARENT_WATCH"

# 语义标记：initial_ppid 必须「启动时读一次」，之后每轮与它比较；
# 绝不能每轮重读当前 ppid（否则永远检测不到变化）。
_INITIAL_SENTINEL = object()   # 仅测试引用：钉住「initial 只读一次」语义

_thread: threading.Thread | None = None
_lock = threading.Lock()


def start_parent_watch(interval: float = 5.0):
    """IFF_PARENT_WATCH=1 时启动守护轮询线程；否则返回 None（生产路径）。

    initial ppid 在此处读一次；后续每轮 getppid() 与之比较。
    """
    global _thread
    if os.environ.get(ENV_VAR) != "1":
        return None
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread          # 幂等：重复调用不叠加线程
        initial_ppid = os.getppid()
        _thread = threading.Thread(
            target=_watch, args=(initial_ppid, interval),
            daemon=True, name="parent-watch")
        _thread.start()
        logging.getLogger("inferfabric.parent_watch").info(
            "parent watch armed (initial ppid=%s, interval=%.1fs) — "
            "instance exits when parent dies", initial_ppid, interval)
        return _thread


def stop_parent_watch() -> None:
    """测试辅助：解除武装（线程本身 daemon 轮询，置空引用即可）。"""
    global _thread
    with _lock:
        _thread = None


def is_armed() -> bool:
    """防护线程是否真的在运行（/health 报告用）。

    报实际状态而非 env 快照：env=1 但线程未起（如装配顺序缺陷）时
    必须能从 /health 看出来，否则防护形同虚设却自报已武装。
    """
    t = _thread
    return t is not None and t.is_alive()


PRODUCTION_PORT = 8999


def instance_banner(port: int, production_port: int = PRODUCTION_PORT) -> str | None:
    """非生产端口的启动横幅（issue #50）：排查双实例时一眼可辨。

    生产端口返回 None（不打扰正常日志）；其他端口返回显眼提示。
    """
    if port == production_port:
        return None
    return ("=" * 60 + "\n"
            "  非生产实例（issue #50 身份标识）\n"
            f"  监听端口: {port}   生产端口: {production_port}\n"
            "  本实例不归 launchd 管理；若为验收实例，请确认\n"
            "  IFF_PARENT_WATCH=1 已设置（父进程消亡自退）\n"
            + "=" * 60)


def _watch(initial_ppid: int, interval: float,
           _sleep=time.sleep, _ppid=os.getppid, _exit=os._exit,
           _max_ticks: int | None = None) -> None:
    """轮询循环（依赖注入供单测）；ppid 变化 = 父死 → 立即退出。

    _max_ticks 仅测试用（限制轮数防测试死循环）；生产恒为 None。
    """
    log = logging.getLogger("inferfabric.parent_watch")
    ticks = 0
    while _max_ticks is None or ticks < _max_ticks:
        ticks += 1
        _sleep(interval)
        current = _ppid()
        if current != initial_ppid:
            log.warning(
                "parent process gone (ppid %s -> %s) — temporary instance "
                "exiting (issue #50 orphan protection)", initial_ppid, current)
            _exit(0)
