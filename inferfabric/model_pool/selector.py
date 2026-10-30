"""inferfabric/model_pool/selector.py — 协议过滤 + 优先级选择 + dry-run 解释。

确定性内核：无随机、无时间依赖、不打网络。每次调用返回完整决策表，
dry-run / status 与真实选择共用同一函数（零副差别名）。

排除原因（CandidateDecision.reason）：
  location_not_allowed  候选位置不在池 allowed_locations
  no_evidence           协议证据不足（UNKNOWN/FAILED/STALE，或 DECLARED 而池未接受）
  feature_unsupported   请求要求的功能不在候选显式声明的约束集内
                        （7.8.0：空声明 = 无约束/全能力默认，不拒；
                        仅非空声明构成严格约束，detail 给 missing/declared 明细）
  local_not_ready       本地候选未激活（ready_only：只选已启动端口，绝不自动启动）
  cloud_model_missing   云模型未在发现列表中
  cooldown              端点冷却中（P2 注入，见 pool_forward.CooldownStore）
  cross_location_disabled 静态兼容但位置非 home（allow_cross_location=false，
                        先于就绪过滤生效，终审 #2）

错误（Selection.error）：
  pool_disabled          池未启用
  no_ready_target        有协议兼容候选但全部未就绪
  no_compatible_target   无协议/功能兼容候选
  unknown_pool           池不存在（dispatch 层产生，本函数不返回）
"""

from dataclasses import dataclass, field

from inferfabric.model_pool.evidence import EvidenceState
from inferfabric.model_pool.registry import PoolSnapshot, ResolvedCandidate


@dataclass(frozen=True)
class RequestRequirements:
    protocol: str          # "openai" | "anthropic"
    stream: bool = False
    tools: bool = False
    vision: bool = False
    structured: bool = False

    def required_features(self) -> tuple[str, ...]:
        out = []
        if self.tools:
            out.append("tools")
        if self.stream:
            out.append("stream")
        if self.vision:
            out.append("vision")
        if self.structured:
            out.append("structured")
        return tuple(out)


@dataclass(frozen=True)
class RuntimeView:
    active_services: set = field(default_factory=set)
    cloud_models: dict = field(default_factory=dict)   # "provider/model_id" → CloudModel
    cooling_fingerprints: frozenset = frozenset()      # P2 冷却端点指纹


@dataclass(frozen=True)
class CandidateDecision:
    candidate_id: str
    included: bool
    reason: str
    # 7.8.0：feature_unsupported 的自解释明细 "missing=<f,...>; declared=[<f,...>]"
    # 其余原因为空串 — 追加字段向后兼容
    detail: str = ""


@dataclass(frozen=True)
class Selection:
    pool: str
    decisions: tuple[CandidateDecision, ...] = ()
    chosen: ResolvedCandidate | None = None
    error: str | None = None
    # P2：全部入选候选（按 priority,id 升序），供回退循环按序尝试
    ordered: tuple[ResolvedCandidate, ...] = ()
    # 静态兼容（含跨位置过滤后的 home 位置约束）的候选数 — 与当前就绪状态
    # 无关。不可迁移历史守卫用它判断「请求是否可能在多目标间迁移」，
    # 不得用 len(ordered)（就绪数会随冷却/宕机波动，终审 #3）
    compatible_count: int = 0


def _evidence_ok(resolved: ResolvedCandidate, protocol: str,
                 accept_declared: bool) -> bool:
    ev = resolved.evidence.get(protocol)
    if ev is None or ev.state == EvidenceState.UNKNOWN:
        return False
    if ev.state == EvidenceState.PROBED:
        return True
    if ev.state == EvidenceState.DECLARED:
        return accept_declared
    return False  # FAILED / STALE


def _static_reason(resolved: ResolvedCandidate, pool, req: RequestRequirements,
                   required_features: tuple[str, ...]) -> tuple[str | None, str]:
    """静态兼容检查（位置/协议证据/功能覆盖）。(None, "") = 兼容。

    与就绪/冷却无关 — 这层结果决定 home 位置与 compatible_count，
    必须先于任何运行时状态过滤计算（终审 #2/#3）。
    返回 (reason, detail)：detail 仅 feature_unsupported 非空（自解释明细）。
    """
    cand = resolved.candidate
    location = "local" if cand.kind == "local" else "cloud"
    if location not in pool.allowed_locations:
        return "location_not_allowed", ""
    if not _evidence_ok(resolved, req.protocol, pool.accept_declared):
        return "no_evidence", ""
    # 功能覆盖以「本次放行的证据」为准（评审 #4）：PROBED 证据只背书
    # 探测过的功能，声明式证据由 registry 镜像 cand.features。
    # 7.8.0 语义反转：空声明 = 无约束（全能力默认）— tools 是 agent
    # 调用的基础，「没声明」不得构成拦截；仅显式非空声明构成严格约束。
    ev = resolved.evidence.get(req.protocol)
    allowed_features = ev.features if ev is not None else ()
    missing = set(required_features) - set(allowed_features)
    if allowed_features and missing:
        detail = (f"missing={','.join(sorted(missing))}; "
                  f"declared=[{', '.join(sorted(allowed_features))}]")
        return "feature_unsupported", detail
    return None, ""


def select(pool_snap: PoolSnapshot, req: RequestRequirements,
           runtime: RuntimeView) -> Selection:
    pool = pool_snap.pool
    if not pool.enabled:
        return Selection(pool=pool.name, error="pool_disabled")

    required_features = req.required_features()
    decisions: list[CandidateDecision] = []
    chosen: ResolvedCandidate | None = None
    included_ordered: list[ResolvedCandidate] = []
    had_protocol_compatible = False
    compatible_count = 0

    # 确定性：按 (priority, id) 升序，不信任调用方传入顺序
    ordered = sorted(pool_snap.candidates,
                     key=lambda r: (r.candidate.priority, r.candidate.id))
    static_reasons = {r.candidate.id: _static_reason(r, pool, req, required_features)
                      for r in ordered}

    # 跨位置回退默认关闭（评审 #6 / 终审 #2）：home 位置以首个「静态兼容」
    # 候选为准，在就绪/冷却过滤之前生效 — 本地主用未就绪或冷却时，
    # 请求明确失败（no_ready_target），绝不静默落到云端备用
    home_loc = None
    if not pool.allow_cross_location:
        for resolved in ordered:
            if static_reasons[resolved.candidate.id][0] is None:
                home_loc = "local" if resolved.candidate.kind == "local" else "cloud"
                break

    for resolved in ordered:
        cand = resolved.candidate
        loc = "local" if cand.kind == "local" else "cloud"
        sreason, sdetail = static_reasons[cand.id]
        if sreason is not None:
            reason = sreason
        elif home_loc is not None and loc != home_loc:
            reason = "cross_location_disabled"
        else:
            compatible_count += 1
            if resolved.endpoint_fingerprint in runtime.cooling_fingerprints:
                reason = "cooldown"
            elif cand.kind == "local" and cand.model_name not in runtime.active_services:
                reason = "local_not_ready"
            elif cand.kind == "cloud" and f"{cand.provider}/{cand.model_id}" not in runtime.cloud_models:
                reason = "cloud_model_missing"
            else:
                reason = "ok"

        # 「协议兼容但未就绪」才有 no_ready_target；功能不匹配属兼容性问题
        # （no_compatible_target），不算未就绪。cross_location_disabled 的
        # 候选协议/功能均兼容，只是位置被策略关掉 — 计入未就绪类
        if reason in ("local_not_ready", "cloud_model_missing", "cooldown",
                      "cross_location_disabled"):
            had_protocol_compatible = True

        included = reason == "ok"
        decisions.append(CandidateDecision(candidate_id=cand.id,
                                           included=included, reason=reason,
                                           detail=sdetail))
        if included and chosen is None:
            chosen = resolved
        if included:
            included_ordered.append(resolved)

    error = None
    if chosen is None:
        if not decisions:
            error = "no_compatible_target"
        elif had_protocol_compatible:
            error = "no_ready_target"
        else:
            error = "no_compatible_target"

    return Selection(pool=pool.name, decisions=tuple(decisions),
                     chosen=chosen, error=error,
                     ordered=tuple(included_ordered),
                     compatible_count=compatible_count)
