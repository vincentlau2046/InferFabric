"""inferfabric/model_pool/selector.py — 协议过滤 + 优先级选择 + dry-run 解释。

确定性内核：无随机、无时间依赖、不打网络。每次调用返回完整决策表，
dry-run / status 与真实选择共用同一函数（零副差别名）。

排除原因（CandidateDecision.reason）：
  location_not_allowed  候选位置不在池 allowed_locations
  no_evidence           协议证据不足（UNKNOWN/FAILED/STALE，或 DECLARED 而池未接受）
  feature_unsupported   请求要求的功能候选未覆盖
  local_not_ready       本地候选未激活（ready_only：只选已启动端口，绝不自动启动）
  cloud_model_missing   云模型未在发现列表中
  cooldown              端点冷却中（P2 注入，见 pool_forward.CooldownStore）

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


@dataclass(frozen=True)
class Selection:
    pool: str
    decisions: tuple[CandidateDecision, ...] = ()
    chosen: ResolvedCandidate | None = None
    error: str | None = None


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


def select(pool_snap: PoolSnapshot, req: RequestRequirements,
           runtime: RuntimeView) -> Selection:
    pool = pool_snap.pool
    if not pool.enabled:
        return Selection(pool=pool.name, error="pool_disabled")

    required_features = req.required_features()
    decisions: list[CandidateDecision] = []
    chosen: ResolvedCandidate | None = None
    had_protocol_compatible = False

    # 确定性：按 (priority, id) 升序，不信任调用方传入顺序
    ordered = sorted(pool_snap.candidates,
                     key=lambda r: (r.candidate.priority, r.candidate.id))
    for resolved in ordered:
        cand = resolved.candidate
        reason = "ok"

        location = "local" if cand.kind == "local" else "cloud"
        if location not in pool.allowed_locations:
            reason = "location_not_allowed"
        elif resolved.endpoint_fingerprint in runtime.cooling_fingerprints:
            reason = "cooldown"
        elif not _evidence_ok(resolved, req.protocol, pool.accept_declared):
            reason = "no_evidence"
        elif not set(required_features) <= set(cand.features):
            reason = "feature_unsupported"
        elif cand.kind == "local" and cand.model_name not in runtime.active_services:
            reason = "local_not_ready"
        elif cand.kind == "cloud" and f"{cand.provider}/{cand.model_id}" not in runtime.cloud_models:
            reason = "cloud_model_missing"

        if reason in ("ok", "feature_unsupported"):
            # 协议证据已过（或仅功能不匹配）→ 视为协议兼容
            if _evidence_ok(resolved, req.protocol, pool.accept_declared) \
                    and location in pool.allowed_locations:
                had_protocol_compatible = True
        elif reason in ("local_not_ready", "cloud_model_missing"):
            had_protocol_compatible = True

        included = reason == "ok"
        decisions.append(CandidateDecision(candidate_id=cand.id,
                                           included=included, reason=reason))
        if included and chosen is None:
            chosen = resolved

    error = None
    if chosen is None:
        if not decisions:
            error = "no_compatible_target"
        elif had_protocol_compatible:
            error = "no_ready_target"
        else:
            error = "no_compatible_target"

    return Selection(pool=pool.name, decisions=tuple(decisions),
                     chosen=chosen, error=error)
