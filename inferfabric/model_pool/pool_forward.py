"""inferfabric/model_pool/pool_forward.py — P2 预算内优先级故障回退。

spec §6.2 固定错误动作表（不可配置化放开「不回退」类别）：
  连接失败/DNS/不可达      → 立即切下一候选（受 max_attempts/deadline）
  429                      → 预算内尊重 Retry-After 等待并重试原目标，
                             超预算切下一候选
  5xx（未提交）            → 最多重试原目标 1 次，之后切下一候选
  401/403（上游凭证错误）   → 端点冷却（CooldownStore，键=endpoint_fingerprint）
                             + 切下一候选
  400/404/422/内容安全等    → 终止，不回退（确定性错误，换候选也无解）
  已提交任何字节            → 永不重试/切换（§6.4，由传输层 committed 信号保证）

spec §6.3 统一预算：max_attempts 计所有真实上游调用（含同目标重试），
deadline 覆盖等待。本模块不 import handler 内部状态、不发客户端响应 —
传输以回调注入，客户端写只发生在传输回调内部。

注：同目标重试（429 等待 / 5xx 一次）与切换共用同一预算；一次 429 等待重试
即消耗 1 attempt，管理员需按池深度调大 max_attempts。
"""

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("inferfabric.model_pool.pool_forward")

DEFAULT_COOLDOWN_TTL_S = 60.0
# 429 无 Retry-After 头时的默认等待（受 deadline 截断）
DEFAULT_RETRY_WAIT_S = 10.0
# Retry-After 上限：不信任上游声明的超长等待
MAX_RETRY_WAIT_S = 30.0


class CooldownStore:
    """端点冷却（内存，进程内）：键 = endpoint_fingerprint，带过期。"""

    def __init__(self, ttl_s: float = DEFAULT_COOLDOWN_TTL_S):
        self._ttl = float(ttl_s)
        self._until: dict[str, float] = {}

    def add(self, fingerprint: str, now: float | None = None) -> None:
        self._until[fingerprint] = (now if now is not None else time.time()) + self._ttl

    def is_cooling(self, fingerprint: str, now: float | None = None) -> bool:
        until = self._until.get(fingerprint)
        if until is None:
            return False
        t = now if now is not None else time.time()
        if t >= until:
            self._until.pop(fingerprint, None)
            return False
        return True

    def cooling_fingerprints(self, now: float | None = None) -> frozenset:
        t = now if now is not None else time.time()
        expired = [fp for fp, until in self._until.items() if t >= until]
        for fp in expired:
            del self._until[fp]
        return frozenset(self._until)


@dataclass
class AttemptResult:
    """单次候选传输的结果（传输回调返回）。

    ok=True 隐含 committed=True（成功响应已完整交付客户端）。
    committed=True 且 ok=False：响应已开始输出后失败 — 截断，不可重试。
    status=None：未收到上游状态（连接失败/未提交即失败）。
    """
    ok: bool = False
    committed: bool = False
    status: int | None = None
    error: str | None = None
    retry_after: float | None = None
    route: str | None = None
    usage: dict = field(default_factory=dict)
    ttft_ms: float | None = None


@dataclass
class AttemptOutcome:
    """整个回退循环的最终结果（dispatch 据此记日志 + 发协议信封）。"""
    ok: bool = False
    committed: bool = False
    status: int | None = None
    error: str | None = None
    # 未提交失败时的 IFF 错误码：pool_unavailable（预算耗尽）；
    # status 非 None 的终止错误由 dispatch 按上游状态发信封
    error_code: str | None = None
    candidate_id: str | None = None
    route: str | None = None
    usage: dict = field(default_factory=dict)
    ttft_ms: float | None = None


def _is_non_fallback(status: int | None) -> bool:
    """确定性错误：换候选无解 → 终止（spec §6.2 不回退类）。"""
    if status is None:
        return False
    if status in (401, 403, 429):
        return False
    if 400 <= status < 500:
        return True          # 400/404/422/内容安全/超限…
    if status == 501:
        return True          # 协议/能力不匹配（云 provider 501 hint 路径）
    return False


def run_attempts(pool, candidates, transport, cooldowns=None, *,
                 monotonic=None, sleep=None) -> AttemptOutcome:
    """按候选序（调用方已排序/过滤）在预算内执行传输并按动作表回退。

    Args:
        pool: PoolConfig（max_attempts / deadline_ms）
        candidates: 有序 eligible ResolvedCandidate 列表
        transport: callable(candidate) -> AttemptResult（每次真实上游调用）
        cooldowns: CooldownStore | None（401/403 时写入端点冷却）
        monotonic/sleep: 可注入时钟/等待（测试用）
    """
    mono = monotonic or time.monotonic
    do_sleep = sleep or time.sleep
    max_attempts = max(1, int(pool.max_attempts))
    deadline = mono() + pool.deadline_ms / 1000.0

    queue = list(candidates)
    retried: set[str] = set()   # 已做同目标重试的候选（5xx 一次 / 429 等待一次）
    attempts = 0
    last: AttemptResult = AttemptResult()
    last_cand = None

    while queue and attempts < max_attempts:
        if mono() >= deadline:
            break
        cand = queue[0]
        last_cand = cand
        attempts += 1
        res = transport(cand)
        last = res

        if res.ok:
            return AttemptOutcome(ok=True, committed=True,
                                  status=res.status or 200,
                                  candidate_id=cand.candidate.id, route=res.route,
                                  usage=res.usage, ttft_ms=res.ttft_ms)
        if res.committed:
            # 响应已提交（含流中断）→ 终止，不重试不切换（§6.4）
            return AttemptOutcome(ok=False, committed=True, status=res.status,
                                  error=res.error, candidate_id=cand.candidate.id,
                                  route=res.route, usage=res.usage,
                                  ttft_ms=res.ttft_ms)
        if res.status in (401, 403):
            if cooldowns is not None:
                cooldowns.add(cand.endpoint_fingerprint)
            log.warning("pool candidate '%s' upstream %s — endpoint cooling",
                        cand.candidate.id, res.status)
            queue.pop(0)
        elif res.status == 429:
            remaining = deadline - mono()
            want = res.retry_after if res.retry_after is not None else DEFAULT_RETRY_WAIT_S
            want = min(want, MAX_RETRY_WAIT_S)
            if want > remaining or cand.candidate.id in retried:
                # 无法在预算内尊重 Retry-After（或已重试过）→ 切下一候选
                queue.pop(0)
            else:
                retried.add(cand.candidate.id)
                log.info("pool candidate '%s' 429 — waiting %.1fs (Retry-After)",
                         cand.candidate.id, want)
                do_sleep(want)   # 同目标重试，受 attempts/deadline 约束
        elif res.status is None:
            # 连接失败/端点不可达 → 立即切下一候选
            queue.pop(0)
        elif 500 <= res.status < 600:
            if cand.candidate.id in retried:
                queue.pop(0)     # 5xx 已重试原目标 1 次 → 切换
            else:
                retried.add(cand.candidate.id)
        elif _is_non_fallback(res.status):
            # 确定性错误 → 终止，不回退
            return AttemptOutcome(ok=False, committed=False, status=res.status,
                                  error=res.error, candidate_id=cand.candidate.id,
                                  route=res.route)
        else:
            # 未归类的未提交失败 → 保守切下一候选
            queue.pop(0)

    return AttemptOutcome(ok=False, committed=False, status=None,
                          error=last.error or "all candidates failed",
                          error_code="pool_unavailable",
                          candidate_id=last_cand.candidate.id if last_cand else None,
                          route=last.route)
