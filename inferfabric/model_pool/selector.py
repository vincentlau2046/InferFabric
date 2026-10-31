"""inferfabric/model_pool/selector.py — 协议过滤 + 优先级选择 + dry-run 解释。

确定性内核：无随机、无时间依赖、不打网络。每次调用返回完整决策表，
dry-run / status 与真实选择共用同一函数（零副差别名）。

7.9.0 语义：能力归模型，池归优先级（LiteLLM Router / OpenRouter 模式）。
vision 是唯一请求感知维度 — vision-capable 判定源是模型本体（本地
models.d model_type=vl → RuntimeView.local_vision；云 supports_vision
=True），池候选 features 字段退役（读时容忍，不参与路由）。tools/
stream/structured 不建模（准入原则下恒真）。无「跳过 VL」规则：无图
请求纯优先级，VL 排最前就全接。

排除原因（CandidateDecision.reason）：
  location_not_allowed  候选位置不在池 allowed_locations
  no_evidence           协议证据不足（UNKNOWN/FAILED/STALE，或 DECLARED 而池未接受）
  feature_unsupported   带图请求但候选模型非 vision-capable（模型本体未
                        声明视觉；detail 给出自解释说明）
  local_not_ready       本地候选未激活（ready_only：只选已启动端口，绝不自动启动）
  cloud_model_missing   云模型未在发现列表中
  cooldown              端点冷却中（P2 注入，见 pool_forward.CooldownStore）
  cross_location_disabled 静态兼容但位置非 home（allow_cross_location=false，
                        先于就绪过滤生效，终审 #2）

错误（Selection.error）：
  pool_disabled          池未启用
  no_ready_target        有协议/视觉兼容候选但全部未就绪
  no_compatible_target   无协议/视觉兼容候选
  unknown_pool           池不存在（dispatch 层产生，本函数不返回）
"""

from dataclasses import dataclass, field

from inferfabric.model_pool.evidence import EvidenceState
from inferfabric.model_pool.registry import PoolSnapshot, ResolvedCandidate


@dataclass(frozen=True)
class RequestRequirements:
    protocol: str          # "openai" | "anthropic"
    vision: bool = False   # 请求携带图片（7.9.0：唯一请求感知维度）


@dataclass(frozen=True)
class RuntimeView:
    active_services: set = field(default_factory=set)
    cloud_models: dict = field(default_factory=dict)   # "provider/model_id" → CloudModel
    cooling_fingerprints: frozenset = frozenset()      # P2 冷却端点指纹
    # vision-capable 本地模型的 served_name 集合（models.d model_type=vl，
    # dispatch.runtime_view 从 mgr 构建 — 选择期零 IO）
    local_vision: frozenset = frozenset()


@dataclass(frozen=True)
class CandidateDecision:
    candidate_id: str
    included: bool
    reason: str
    # feature_unsupported 的自解释明细（7.9.0：视觉语义）
    # 其余原因为空串
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


def _vision_capable(resolved: ResolvedCandidate,
                    runtime: RuntimeView) -> bool | None:
    """候选模型是否 vision-capable。True/False = 已知；None = 未知
    （云模型未发现 — 不做静态视觉判定，交给就绪层报精确理由）。"""
    cand = resolved.candidate
    if cand.kind == "local":
        return cand.model_name in runtime.local_vision
    cm = runtime.cloud_models.get(f"{cand.provider}/{cand.model_id}")
    if cm is None:
        return None
    return getattr(cm, "supports_vision", None) is True


def _static_reason(resolved: ResolvedCandidate, pool, req: RequestRequirements,
                   runtime: RuntimeView) -> tuple[str | None, str]:
    """静态兼容检查（位置/协议证据/视觉能力）。(None, "") = 兼容。

    与就绪/冷却无关 — 这层结果决定 home 位置与 compatible_count，
    必须先于任何运行时状态过滤计算（终审 #2/#3）。
    返回 (reason, detail)：detail 仅 feature_unsupported 非空（自解释）。
    """
    cand = resolved.candidate
    location = "local" if cand.kind == "local" else "cloud"
    if location not in pool.allowed_locations:
        return "location_not_allowed", ""
    if not _evidence_ok(resolved, req.protocol, pool.accept_declared):
        return "no_evidence", ""
    # 7.9.0：带图请求只在 vision-capable 候选中路由。能力判定源 =
    # 模型本体（local_vision / supports_vision），与池候选声明无关。
    if req.vision and _vision_capable(resolved, runtime) is False:
        return ("feature_unsupported",
                "request requires vision; model is not vision-capable")
    return None, ""


def select(pool_snap: PoolSnapshot, req: RequestRequirements,
           runtime: RuntimeView) -> Selection:
    pool = pool_snap.pool
    if not pool.enabled:
        return Selection(pool=pool.name, error="pool_disabled")

    decisions: list[CandidateDecision] = []
    chosen: ResolvedCandidate | None = None
    included_ordered: list[ResolvedCandidate] = []
    had_protocol_compatible = False
    compatible_count = 0

    # 确定性：按 (priority, id) 升序，不信任调用方传入顺序
    ordered = sorted(pool_snap.candidates,
                     key=lambda r: (r.candidate.priority, r.candidate.id))
    static_reasons = {r.candidate.id: _static_reason(r, pool, req, runtime)
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
