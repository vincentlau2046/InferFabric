"""model_pool.history — P3 数据底盘：每池每候选延迟环（内存，进程内）。

PoolHistory 只记「成功且有 TTFT」的真实请求（失败/无 TTFT 不记 —
延迟视图回答的是「这个候选正常服务时有多快」，故障已有事件环与
冷却状态承载）。每候选环形上限（默认 40），新在后。

线程安全：多请求线程并发 record；snapshot 锁内深拷贝结构。
零转发行为变化：run_attempts 可选注入（缺省 None 不旁录）。
"""

import threading
import time
from collections import deque

# 每候选环上限：C1 迷你图（Task 8）取最近 ~20 点，40 上限留余量
# 且限制单候选内存占用
MAX_PER_CANDIDATE = 40


class PoolHistory:
    """池 → 候选 → 延迟环。record 过滤 + snapshot 分组均在锁内。"""

    def __init__(self, max_per_candidate: int = MAX_PER_CANDIDATE,
                 clock=None):
        self._clock = clock or time.time
        self._max = max(1, int(max_per_candidate))
        # (pool, candidate_id) → deque[{ts, ttft_ms, total_ms}]
        self._rings: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def record(self, pool: str, candidate_id: str, ttft_ms: float,
               total_ms: float, ok: bool) -> None:
        """只记成功且有 TTFT 的请求（调用方已过滤，此处兜底双保险）。"""
        if not ok or ttft_ms is None:
            return
        with self._lock:
            ring = self._rings.setdefault(
                (pool, candidate_id), deque(maxlen=self._max))
            ring.append({"ts": self._clock(),
                         "ttft_ms": float(ttft_ms),
                         "total_ms": float(total_ms)})

    def snapshot(self, now: float | None = None) -> dict:
        """锁内快照：{pool: {candidate_id: [{age_s, wall_ts, ttft_ms, total_ms}]}}。

        新在后；age_s/wall_ts 同源（记录时刻的墙钟）。
        """
        t = self._clock() if now is None else now
        with self._lock:
            out: dict = {}
            for (pool, cid), ring in self._rings.items():
                out.setdefault(pool, {})[cid] = [
                    {"age_s": t - e["ts"], "wall_ts": e["ts"],
                     "ttft_ms": e["ttft_ms"], "total_ms": e["total_ms"]}
                    for e in ring
                ]
            return out
