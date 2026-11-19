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
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("inferfabric.model_pool.pool_forward")

DEFAULT_COOLDOWN_TTL_S = 60.0
# 每指纹最近尝试记录上限（终审 M8）：池配置规模远低于此，仅防配置
# 演变导致的指纹累积
MAX_LAST_ATTEMPTS = 512
# 事件环全局上限（P3 C2 时间线）：跨指纹共享，新在后，超限淘汰最旧
MAX_EVENTS = 256
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

    def __init__(self, ttl_s: float = DEFAULT_COOLDOWN_TTL_S,
                 max_last_entries: int = MAX_LAST_ATTEMPTS):
        self._ttl = float(ttl_s)
        # _last 上限（终审 M8）：指纹随配置演变不断新增，无上限则单调
        # 增长；超限时按记录时刻淘汰最旧条目
        self._max_last = max(1, int(max_last_entries))
        self._until: dict[str, float] = {}
        # 冷却原因（v7.1.0 运行态视图）：fp → "cooldown_401"/"cooldown_403"/
        # "quarantine_404"；None = 既有调用未带原因（向后兼容）
        self._reason: dict[str, str | None] = {}
        # 每 fp 最近一次真实传输尝试 (ok, status, mono 时刻) — 供 runtime 端点
        # 展示「最近失败」；admission 跳过不算尝试
        self._last: dict[str, tuple[bool, int | None, float]] = {}
        self._probing: set[str] = set()
        # 事件环（P3 C2 时间线）：{mono, wall, fingerprint, type, reason}，
        # 新在后，全局 deque(maxlen) 封顶
        self._events: deque = deque(maxlen=MAX_EVENTS)
        self._lock = threading.Lock()

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def _record_event(self, fingerprint: str, type_: str,
                      reason: str | None, mono: float) -> None:
        """锁内追加事件（调用方必须已持锁）。wall = 真实墙钟（展示用）。"""
        self._events.append({
            "mono": mono, "wall": time.time(),
            "fingerprint": fingerprint, "type": type_, "reason": reason,
        })

    def add(self, fingerprint: str, now: float | None = None,
            ttl: float | None = None, reason: str | None = None,
            *, _event_type: str = "cooldown") -> None:
        with self._lock:
            t = self._now(now)
            self._until[fingerprint] = t + (ttl if ttl is not None else self._ttl)
            self._reason[fingerprint] = reason
            self._probing.discard(fingerprint)
            self._record_event(fingerprint, _event_type, reason, t)

    def quarantine(self, fingerprint: str, now: float | None = None) -> None:
        """404 隔离：长 TTL 冷却（模型不存在，换请求也无解）。

        事件记 quarantine（不双记 cooldown）。
        """
        self.add(fingerprint, now=now, ttl=QUARANTINE_TTL_S,
                 reason="quarantine_404", _event_type="quarantine")

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
            # 授予到期探测（健康端点放行不在此路径，不记事件）
            self._record_event(fingerprint, "probe_start",
                               self._reason.get(fingerprint), self._now(now))
            return True

    def finish_probe(self, fingerprint: str, ok: bool,
                     now: float | None = None) -> None:
        """探测结果回写：ok → 完全恢复（删条目）；失败 → 重新冷却。
        非探测（未注册）调用为 no-op。"""
        with self._lock:
            if fingerprint not in self._probing:
                return
            self._probing.discard(fingerprint)
            # 注册态回写才记事件（非探测 no-op 不记）；失败重新冷却
            # 直接写 _until（不 double 记 cooldown）
            self._record_event(fingerprint, "probe_pass" if ok else "probe_fail",
                               self._reason.get(fingerprint), self._now(now))
            if ok:
                self._until.pop(fingerprint, None)
                self._reason.pop(fingerprint, None)
            else:
                # 重新冷却保留原 reason（新故障原因由 add() 覆盖，探测失败不覆盖）
                self._until[fingerprint] = self._now(now) + self._ttl

    def release_probe(self, fingerprint: str, now: float | None = None) -> None:
        """释放探测权但不回写结果（PLAN-2026-11-15 v2 Task 4）。

        适用：探测领取后请求在真实上游调用前退出（未准入 / 传输异常 /
        客户端取消）。端点未被证实成功或失败 — 保持「到期待探测」状态
        （_until 不动），下一个请求可重新领取；绝不误记端点失败。
        """
        with self._lock:
            if fingerprint not in self._probing:
                return
            self._probing.discard(fingerprint)
            self._record_event(fingerprint, "probe_cancel",
                               self._reason.get(fingerprint), self._now(now))

    # ── 运行态只读视图（v7.1.0，供 GET /admin/model-pools/runtime）──

    def entries(self, now: float | None = None) -> dict[str, dict]:
        """锁内快照：fp → {until, remaining_s, reason, probing}。

        含已到期未探测条目（半开语义要求条目保留）：remaining_s <= 0 且
        probing=False 表示「到期待探测」；probing=True 表示探测在途。
        """
        with self._lock:
            t = self._now(now)
            return {
                fp: {
                    "until": until,
                    "remaining_s": until - t,
                    "reason": self._reason.get(fp),
                    "probing": fp in self._probing,
                }
                for fp, until in self._until.items()
            }

    def record_attempt(self, fingerprint: str, ok: bool,
                       status: int | None = None,
                       now: float | None = None) -> None:
        """记录 fp 最近一次真实传输尝试（覆盖式，每 fp 只留一条）。

        新指纹且已达上限时淘汰最旧条目（终审 M8 — 防单调增长）。
        """
        with self._lock:
            if (fingerprint not in self._last
                    and len(self._last) >= self._max_last):
                oldest = min(self._last, key=lambda fp: self._last[fp][2])
                del self._last[oldest]
            self._last[fingerprint] = (bool(ok), status, self._now(now))

    def last_attempts(self, now: float | None = None) -> dict[str, dict]:
        """锁内快照：fp → {ok, status, age_s}。"""
        with self._lock:
            t = self._now(now)
            return {
                fp: {"ok": ok, "status": status, "age_s": t - at}
                for fp, (ok, status, at) in self._last.items()
            }

    def events(self, now: float | None = None) -> list[dict]:
        """锁内快照（P3 C2 时间线）：事件环，新在后。

        条目：{age_s, wall_ts, fingerprint, type, reason}；age_s 由注入
        钟算（与 store 其余视图同源），wall_ts 为记录时刻真实墙钟。
        """
        with self._lock:
            t = self._now(now)
            return [
                {"age_s": t - e["mono"], "wall_ts": e["wall"],
                 "fingerprint": e["fingerprint"], "type": e["type"],
                 "reason": e["reason"]}
                for e in self._events
            ]


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
    # 候选能力不匹配本请求（传输层信号，2026-11-04）：如本地引擎 400
    # code=parallel_tool_use_not_supported — 换候选可解（云接受该字段），
    # 但不冷却端点（其他请求形状不受影响）。False = 固定动作表不变。
    incompatible: bool = False
    route: str | None = None
    usage: dict = field(default_factory=dict)
    ttft_ms: float | None = None
    # 流结束分类（Task 3）：completed / missing_terminal / error_event /
    # read_error / client_disconnect；非流式 None。布尔 committed 不再独自
    # 决定 ok — 流缺终止标志/显式错误事件 = 未完成
    stream_state: str | None = None


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
    incompatible: bool = False
    duration_ms: float | None = None
    # 观测字段（Task 3）：stream 结束分类、attempt 级 TTFT 与 usage 未知语义
    stream_state: str | None = None
    ttft_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    usage_known: bool | None = None


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


# 内部固定错误前缀（非上游错误体）：这些文本由本层构造，不含上游回包内容
_INTERNAL_ERROR_PREFIXES = (
    "cooldown_admission", "transport error", "physical target",
    "cloud target", "local target", "stream read error",
)


def safe_attempt_error(rec: "AttemptRecord") -> str | None:
    """attempt 错误固定码化（终审 C1）。

    上游错误体可能回显凭证/请求片段（部分 provider 对 401 回显 API key），
    原文绝不入 pool_summary（→ RequestLog metadata）与进程日志 — 与
    终审 #16（RequestLog error 列固定码）同一纪律延伸到 attempt 级。
    status/stream 分类足够定位；原文调试需求由 status + candidate_id +
    时间戳在上游侧自查满足。"""
    if not rec.error:
        return None
    if rec.status is not None:
        return f"upstream_http_{rec.status}"
    if rec.stream_state:
        return f"stream_{rec.stream_state}"
    if rec.error.startswith(_INTERNAL_ERROR_PREFIXES):
        # 内部文本：截掉冒号后的细节（异常串/gate 原因），保留类别
        return rec.error.split(":", 1)[0]
    return "attempt_error"


def _usage_observations(usage: dict | None, ok: bool) -> tuple:
    """AttemptResult.usage → (tokens_in, tokens_out, usage_known)。

    观测器以全零 dict 初始化，全零 ≠ 真实 0 — 任一 token 字段非零才算
    已知（Task 3 契约：usage 缺失 = usage_known False/None，不伪造 0）。
    成功但无 usage → usage_known=False；失败尝试无 usage → None（未知）。
    极端边界（真实 0/0 响应）按缺失处理 — 宁可 unknown 不冒认。
    """
    u = usage or {}
    if u.get("prompt_tokens") or u.get("completion_tokens"):
        return u.get("prompt_tokens"), u.get("completion_tokens"), True
    return None, None, (False if ok else None)


def _target_identity(cand) -> dict:
    """候选 → pool_summary 目标身份（v1 契约：candidate_id/provider/
    requested_model_id/actual_model_id；本层未区分请求别名，二者同值，
    Task 3 结构化传输接续细化）。local 无 provider → None。"""
    c = cand.candidate
    if c.kind == "cloud":
        mid = c.model_id
        prov = c.provider
    else:
        mid = c.model_name
        prov = None
    return {"candidate_id": c.id, "provider": prov,
            "requested_model_id": mid, "actual_model_id": mid}


def build_pool_summary(outcome: "AttemptOutcome", *, pool_name: str,
                       protocol: str, config_revision: int,
                       requested_model: str,
                       candidates=()) -> dict:
    """AttemptOutcome → metadata.pool_summary v1（PLAN-2026-11-15 v2 §3.2）。

    - attempt_count 只数真实上游调用（admitted=True，终审 #11）；
      retry_count=同目标连续再调用，fallback_count=切换目标后再调用。
    - first_attempted/final_target 取首个/最后一个 admitted attempt 的
      候选身份；无 admitted attempt 时为 None（明确 null，不冒认）。
    - 逐 attempt 精简记录；stream/tokens/usage_known 结构化传输未贯通
      前明确 null（Task 3 接续），不伪造 0。
    """
    cands = {r.candidate.id: r for r in candidates}
    admitted = [r for r in outcome.attempts if r.admitted]
    retry_count = fallback_count = 0
    prev = None
    for r in admitted:
        if prev is not None:
            if r.candidate_id == prev:
                retry_count += 1
            else:
                fallback_count += 1
        prev = r.candidate_id

    def _target(cid):
        rc = cands.get(cid)
        return _target_identity(rc) if rc is not None else {"candidate_id": cid}

    if not outcome.ok:
        if outcome.committed:
            outcome_name = "committed_error"
        elif outcome.status is not None:
            outcome_name = "upstream_error"
        else:
            outcome_name = "pool_unavailable"
    else:
        outcome_name = "ok"

    return {
        "v": 1,
        "pool": pool_name,
        "protocol": protocol,
        "config_revision": config_revision,
        "requested_model": requested_model,
        "first_attempted": (_target(admitted[0].candidate_id)
                            if admitted else None),
        "final_target": (_target(admitted[-1].candidate_id)
                         if admitted else None),
        "attempt_count": len(admitted),
        "retry_count": retry_count,
        "fallback_count": fallback_count,
        "outcome": outcome_name,
        "attempts": [{
            "ordinal": r.ordinal, "candidate_id": r.candidate_id,
            "route": r.route, "status": r.status,
            # 终审 C1：固定码 — 上游错误体原文不入摘要（→ RequestLog metadata）
            "error": safe_attempt_error(r),
            "committed": r.committed, "ok": r.ok, "admitted": r.admitted,
            "duration_ms": r.duration_ms,
            # 流结束分类与 attempt 级观测（Task 3；无观测时明确 null）
            "stream": r.stream_state, "ttft_ms": r.ttft_ms,
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out,
            "usage_known": r.usage_known,
        } for r in outcome.attempts],
    }


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
                 monotonic=None, sleep=None, deadline_start=None,
                 history=None) -> AttemptOutcome:
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

        # 预算/次数已尽先于探测领取（Task 4）：先领后判会把探测权困在
        # 已耗尽的循环里 — 不领取就不会泄漏
        if attempts >= max_attempts:
            break

        # 准入 1/2 — 端点冷却/半开单飞（终审 #18）：仍在冷却或已有并发
        # 探测在途 → 跳过该候选。准入失败不是真实上游调用，不消耗预算。
        if cooldowns is not None and not cooldowns.claim_probe(
                cand.endpoint_fingerprint, now=mono()):
            records.append(AttemptRecord(
                ordinal=len(records) + 1, candidate_id=cand.candidate.id,
                error="cooldown_admission", admitted=False))
            queue.pop(0)
            continue

        t0 = mono()
        try:
            res = transport(cand, remaining)
        except Exception as e:
            # 传输层异常（Task 4）：探测权释放但不回写失败（端点未被证实），
            # 按连接失败动作表切下一候选 — 异常不得把半开探测权困死到重启。
            # 终审 I7：异常 = 可能已发出的真实尝试（连接失败与发送后超时在
            # 此层不可区分，保守计入）→ admitted=True + 消耗 max_attempts，
            # attempt_count 不再少报、物理调用数受预算约束。准入失败
            # （下方 res.admitted=False，发送前边界）仍不计。
            log.warning("pool transport raised for candidate '%s': %s",
                        cand.candidate.id, e)
            if cooldowns is not None:
                cooldowns.release_probe(cand.endpoint_fingerprint, now=mono())
            attempts += 1
            records.append(AttemptRecord(
                ordinal=len(records) + 1, candidate_id=cand.candidate.id,
                error=f"transport error: {e}", admitted=True,
                duration_ms=(mono() - t0) * 1000.0))
            queue.pop(0)
            continue

        # 传输耗时已消耗预算：重算剩余（Task 4）— 429 等待/后续决策不得
        # 使用循环顶部的 stale remaining
        remaining = deadline - mono()

        # 准入 2/2 — 传输回调判定准入失败（gate 超时/端口预留失败等，
        # 终审 #11）：不消耗 attempt 预算，切下一候选。探测权释放但不
        # 回写失败（Task 4：拒绝准入不误记端点失败）
        if not res.admitted:
            if cooldowns is not None:
                cooldowns.release_probe(cand.endpoint_fingerprint, now=mono())
            records.append(AttemptRecord(
                ordinal=len(records) + 1, candidate_id=cand.candidate.id,
                route=res.route, error=res.error or "not_admitted",
                admitted=False, duration_ms=(mono() - t0) * 1000.0))
            queue.pop(0)
            continue

        attempts += 1
        last = res
        tokens_in, tokens_out, usage_known = _usage_observations(res.usage, res.ok)
        records.append(AttemptRecord(
            ordinal=len(records) + 1, candidate_id=cand.candidate.id, route=res.route,
            status=res.status, error=res.error, committed=res.committed,
            ok=res.ok, retry_after=res.retry_after, incompatible=res.incompatible,
            duration_ms=(mono() - t0) * 1000.0,
            stream_state=res.stream_state, ttft_ms=res.ttft_ms,
            tokens_in=tokens_in, tokens_out=tokens_out,
            usage_known=usage_known))

        # 半开探测结果回写（健康端点未注册探测 → no-op，终审 #18）。
        # incompatible 视作探测通过（cross-review 2026-11-04）：端点有应答、
        # 只是拒绝这个请求形状 — 探测撞上它不得把健康端点续冷一整个 TTL
        if cooldowns is not None:
            # 运行态视图：记录最近一次真实传输尝试（v7.1.0；与探测回写同钟）
            cooldowns.record_attempt(cand.endpoint_fingerprint, res.ok,
                                     res.status, now=mono())
            cooldowns.finish_probe(cand.endpoint_fingerprint,
                                   res.ok or res.incompatible, now=mono())

        if res.ok:
            # P3 数据底盘：延迟环旁录（只记成功且有 TTFT；缺省 None 不旁录）
            if history is not None and res.ttft_ms is not None:
                history.record(pool.name, cand.candidate.id, res.ttft_ms,
                               records[-1].duration_ms, True)
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
        if res.incompatible and res.status == 400:
            # 候选能力不匹配本请求（2026-11-04）：本地引擎不支持请求中的
            # 某个 API 选项（如 parallel_tool_use_not_supported）→ 该候选
            # 确定无法服务本请求，切下一候选。不冷却端点：引擎拒绝的是
            # 这个请求形状，其他请求（无该选项）照常服务。真实状态保留
            # 在 records 供观测（_log_failed_attempts 会告警该次尝试）。
            # 状态守卫（cross-review）：incompatible 只对 400 生效 — 带
            # incompatible 的 429/5xx/401 继续走下方既有动作表，绝不允许
            # 该信号绕过 Retry-After 等待或 5xx 同目标重试。
            log.info("pool candidate '%s' incompatible for this request "
                     "(status=%s error=%s) — trying next candidate",
                     cand.candidate.id, res.status, res.error)
            queue.pop(0)
        elif res.status in (401, 403):
            if cooldowns is not None:
                cooldowns.add(cand.endpoint_fingerprint, now=mono(),
                              reason=f"cooldown_{res.status}")
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
