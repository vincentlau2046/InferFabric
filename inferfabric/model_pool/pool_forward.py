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
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("inferfabric.model_pool.pool_forward")

DEFAULT_COOLDOWN_TTL_S = 60.0
# 404 隔离 TTL：模型/路由已不存在，长冷却避免每请求都撞 404（终审 #18）
QUARANTINE_TTL_S = 1800.0
# 429 无 Retry-After 头时的默认等待（受 deadline 截断）
DEFAULT_RETRY_WAIT_S = 10.0
# Retry-After 上限：超过 → 不缩短、直接切下一候选（终审 #12）
MAX_RETRY_WAIT_S = 30.0


class CooldownStore:
    """端点冷却（内存，进程内）：键 = endpoint_fingerprint，带过期 + 半开单飞。

    线程安全（终审 #17）：多请求线程并发读写同一 store。
    半开恢复（终审 #18）：到期条目不再被扫描清除，改为 claim_probe 单飞
    探测 — 首个请求验证端点，其余请求在探测完成前继续视作冷却；
    探测失败重新冷却，成功才完全恢复。到期条目不上报
    cooling_fingerprints（selector 被动恢复语义不变）。
    """

    def __init__(self, ttl_s: float = DEFAULT_COOLDOWN_TTL_S):
        self._ttl = float(ttl_s)
        self._until: dict[str, float] = {}
        self._probing: set[str] = set()
        self._lock = threading.Lock()

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def add(self, fingerprint: str, now: float | None = None,
            ttl: float | None = None) -> None:
        with self._lock:
            self._until[fingerprint] = self._now(now) + (ttl if ttl is not None else self._ttl)
            self._probing.discard(fingerprint)

    def quarantine(self, fingerprint: str, now: float | None = None) -> None:
        """404 隔离：长 TTL 冷却（模型不存在，换请求也无解）。"""
        self.add(fingerprint, now=now, ttl=QUARANTINE_TTL_S)

    def is_cooling(self, fingerprint: str, now: float | None = None) -> bool:
        with self._lock:
            until = self._until.get(fingerprint)
            if until is None:
                return False
            # 到期条目保留（半开探测依赖），不在此清除
            return self._now(now) < until

    def cooling_fingerprints(self, now: float | None = None) -> frozenset:
        with self._lock:
            t = self._now(now)
            return frozenset(fp for fp, until in self._until.items() if t < until)

    def claim_probe(self, fingerprint: str, now: float | None = None) -> bool:
        """半开单飞探测权（终审 #18）。

        - 冷却中 → False
        - 到期且无探测在途 → True（注册探测权；并发其余请求 False）
        - 无条目（健康端点）→ True（放行；不注册 — finish_probe 为 no-op，
          健康端点的成功/失败不得影响其他请求写入的冷却状态）
        """
        with self._lock:
            until = self._until.get(fingerprint)
            if until is None:
                return True
            if self._now(now) < until:
                return False
            if fingerprint in self._probing:
                return False
            self._probing.add(fingerprint)
            return True

    def finish_probe(self, fingerprint: str, ok: bool,
                     now: float | None = None) -> None:
        """探测结果回写：ok → 完全恢复（删条目）；失败 → 重新冷却。
        非探测（未注册）调用为 no-op。"""
        with self._lock:
            if fingerprint not in self._probing:
                return
            self._probing.discard(fingerprint)
            if ok:
                self._until.pop(fingerprint, None)
            else:
                self._until[fingerprint] = self._now(now) + self._ttl


@dataclass
class AttemptResult:
    """单次候选传输的结果（传输回调返回）。

    ok=True 隐含 committed=True（成功响应已完整交付客户端）。
    committed=True 且 ok=False：响应已开始输出后失败 — 截断，不可重试。
    status=None：未收到上游状态（连接失败/未提交即失败）。
    admitted=False：准入失败（gate 超时/端口预留失败等）— 未发生真实
    上游调用，不消耗 attempt 预算（终审 #11）。
    """
    ok: bool = False
    committed: bool = False
    admitted: bool = True
    status: int | None = None
    error: str | None = None
    retry_after: float | None = None
    route: str | None = None
    usage: dict = field(default_factory=dict)
    ttft_ms: float | None = None


@dataclass
class AttemptRecord:
    """单次真实上游调用的结构化记录（spec §9.2 逐 attempt 观测）。

    admitted=False 的记录是准入失败（未发生上游调用），不计入
    max_attempts 预算（终审 #11）。
    """
    ordinal: int
    candidate_id: str
    route: str | None = None
    status: int | None = None
    error: str | None = None
    committed: bool = False
    ok: bool = False
    admitted: bool = True
    retry_after: float | None = None
    duration_ms: float | None = None


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
    # 最终目标的物理身份（终审 #15 费用归属）：local=served_name /
    # cloud=model_id；cloud_provider 为云 provider 名（local 为 None）。
    # RequestLog 的 model/cloud_provider 字段按此填写 — 逻辑名只进 route。
    physical_model: str | None = None
    cloud_provider: str | None = None
    usage: dict = field(default_factory=dict)
    ttft_ms: float | None = None
    # 全部真实上游调用（含成功/失败/终止），按发生顺序
    attempts: list[AttemptRecord] = field(default_factory=list)


def _physical_identity(cand) -> tuple[str | None, str | None]:
    c = cand.candidate
    if c.kind == "cloud":
        return c.model_id, c.provider
    return c.model_name, None


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
                 monotonic=None, sleep=None, deadline_start=None) -> AttemptOutcome:
    """按候选序（调用方已排序/过滤）在预算内执行传输并按动作表回退。

    Args:
        pool: PoolConfig（max_attempts / deadline_ms）
        candidates: 有序 eligible ResolvedCandidate 列表
        transport: callable(candidate, remaining) -> AttemptResult。
            remaining = 剩余预算秒数 — 传输层用它约束连接/读超时与
            gate 排队等待，不得超预算（终审 #7）。
        cooldowns: CooldownStore | None（401/403 冷却、404 隔离、
            半开探测准入/回写，终审 #18）
        monotonic/sleep: 可注入时钟/等待（测试用）
        deadline_start: 预算起点（monotonic 时钟）。None = 循环开始时刻。
            调用方在「限流排队之前」捕获并传入 — 排队等待计入预算（§6.3）。
    """
    mono = monotonic or time.monotonic
    do_sleep = sleep or time.sleep
    max_attempts = max(1, int(pool.max_attempts))
    deadline = ((deadline_start if deadline_start is not None else mono())
                + pool.deadline_ms / 1000.0)

    queue = list(candidates)
    retried: set[str] = set()   # 已做同目标重试的候选（5xx 一次 / 429 等待一次）
    attempts = 0
    records: list[AttemptRecord] = []
    last: AttemptResult = AttemptResult()
    last_cand = None

    while queue:
        remaining = deadline - mono()
        if remaining <= 0:
            break
        cand = queue[0]
        last_cand = cand

        # 准入 1/2 — 端点冷却/半开单飞（终审 #18）：仍在冷却或已有并发
        # 探测在途 → 跳过该候选。准入失败不是真实上游调用，不消耗预算。
        if cooldowns is not None and not cooldowns.claim_probe(
                cand.endpoint_fingerprint, now=mono()):
            records.append(AttemptRecord(
                ordinal=len(records) + 1, candidate_id=cand.candidate.id,
                error="cooldown_admission", admitted=False))
            queue.pop(0)
            continue

        if attempts >= max_attempts:
            break

        t0 = mono()
        res = transport(cand, remaining)

        # 准入 2/2 — 传输回调判定准入失败（gate 超时/端口预留失败等，
        # 终审 #11）：不消耗 attempt 预算，切下一候选
        if not res.admitted:
            records.append(AttemptRecord(
                ordinal=len(records) + 1, candidate_id=cand.candidate.id,
                route=res.route, error=res.error or "not_admitted",
                admitted=False, duration_ms=(mono() - t0) * 1000.0))
            queue.pop(0)
            continue

        attempts += 1
        last = res
        records.append(AttemptRecord(
            ordinal=len(records) + 1, candidate_id=cand.candidate.id, route=res.route,
            status=res.status, error=res.error, committed=res.committed,
            ok=res.ok, retry_after=res.retry_after,
            duration_ms=(mono() - t0) * 1000.0))

        # 半开探测结果回写（健康端点未注册探测 → no-op，终审 #18）
        if cooldowns is not None:
            cooldowns.finish_probe(cand.endpoint_fingerprint, res.ok, now=mono())

        if res.ok:
            pm_id, prov = _physical_identity(cand)
            return AttemptOutcome(ok=True, committed=True,
                                  status=res.status or 200,
                                  candidate_id=cand.candidate.id, route=res.route,
                                  physical_model=pm_id, cloud_provider=prov,
                                  usage=res.usage, ttft_ms=res.ttft_ms,
                                  attempts=records)
        if res.committed:
            # 响应已提交（含流中断）→ 终止，不重试不切换（§6.4）
            pm_id, prov = _physical_identity(cand)
            return AttemptOutcome(ok=False, committed=True, status=res.status,
                                  error=res.error, candidate_id=cand.candidate.id,
                                  route=res.route, physical_model=pm_id,
                                  cloud_provider=prov, usage=res.usage,
                                  ttft_ms=res.ttft_ms, attempts=records)
        if res.status in (401, 403):
            if cooldowns is not None:
                cooldowns.add(cand.endpoint_fingerprint, now=mono())
            log.warning("pool candidate '%s' upstream %s — endpoint cooling",
                        cand.candidate.id, res.status)
            queue.pop(0)
        elif res.status == 429:
            want = res.retry_after if res.retry_after is not None else DEFAULT_RETRY_WAIT_S
            # Retry-After 不得缩短（终审 #12）：上游要求的等待超过上限或
            # 剩余预算 → 切下一候选，绝不按截断值提前重试原目标
            if want > MAX_RETRY_WAIT_S or want > remaining or cand.candidate.id in retried:
                queue.pop(0)
            else:
                retried.add(cand.candidate.id)
                log.info("pool candidate '%s' 429 — waiting %.1fs (Retry-After)",
                         cand.candidate.id, want)
                do_sleep(want)   # 同目标重试，受 attempts/deadline 约束
        elif _is_non_fallback(res.status):
            # 确定性错误（4xx/501）→ 终止，不回退。先于 5xx 分支判定：
            # 501 属「不回退」类，落入 5xx 重试分支会变成可回退（评审 #12）。
            # 404 = 目标不存在（模型下线/路由变更）→ 端点隔离长冷却，
            # 后续请求不再把它当可尝试目标（终审 #18）
            if res.status == 404 and cooldowns is not None:
                cooldowns.quarantine(cand.endpoint_fingerprint, now=mono())
                log.warning("pool candidate '%s' upstream 404 — endpoint quarantined",
                            cand.candidate.id)
            pm_id, prov = _physical_identity(cand)
            return AttemptOutcome(ok=False, committed=False, status=res.status,
                                  error=res.error, candidate_id=cand.candidate.id,
                                  route=res.route, physical_model=pm_id,
                                  cloud_provider=prov, attempts=records)
        elif res.status is None:
            # 连接失败/端点不可达 → 立即切下一候选
            queue.pop(0)
        elif 500 <= res.status < 600:
            if cand.candidate.id in retried:
                queue.pop(0)     # 5xx 已重试原目标 1 次 → 切换
            else:
                retried.add(cand.candidate.id)
        else:
            # 未归类的未提交失败 → 保守切下一候选
            queue.pop(0)

    pm_id, prov = _physical_identity(last_cand) if last_cand else (None, None)
    return AttemptOutcome(ok=False, committed=False, status=None,
                          error=last.error or "all candidates failed",
                          error_code="pool_unavailable",
                          candidate_id=last_cand.candidate.id if last_cand else None,
                          route=last.route, physical_model=pm_id,
                          cloud_provider=prov, attempts=records)
