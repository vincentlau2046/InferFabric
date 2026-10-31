"""model_pool.selector — 协议过滤 + 优先级排序 + dry-run 解释。"""

from inferfabric.model_pool.config import CandidateConfig, PoolConfig
from inferfabric.model_pool.evidence import Evidence, EvidenceState
from inferfabric.model_pool.registry import PoolSnapshot, ResolvedCandidate
from inferfabric.model_pool.selector import (
    RuntimeView,
    Selection,
    RequestRequirements,
    select,
)


def _cand(cid, priority, kind="cloud", protocols=("openai",), features=(),
          model_name="m", provider="p", model_id=None):
    return CandidateConfig(
        id=cid, priority=priority, kind=kind, model_name=model_name,
        provider=provider, model_id=model_id or f"{cid}-model",
        declared_protocols=tuple(protocols), features=tuple(features))


def _resolved(cand, states=("PROBED",)):
    """states: 单个状态广播到全部声明协议；否则与 declared_protocols 对齐。"""
    if len(states) == 1:
        states = states * len(cand.declared_protocols)
    evidence = {}
    for protocol, state in zip(cand.declared_protocols, states):
        evidence[protocol] = Evidence(
            state=EvidenceState[state], protocol=protocol,
            features=cand.features, fingerprint=f"fp-{cand.id}",
            recorded_at="t", source="admin-probe")
    return ResolvedCandidate(candidate=cand, endpoint_fingerprint=f"fp-{cand.id}",
                             evidence=evidence)


def _pool_snap(cands, enabled=True, allowed=("cloud", "local"), accept_declared=False,
               cross=False):
    pool = PoolConfig(
        name="fast", enabled=enabled, allowed_locations=tuple(allowed),
        accept_declared=accept_declared, allow_cross_location=cross,
        candidates=tuple(cands))
    return PoolSnapshot(revision=1, pool=pool,
                        candidates=tuple(_resolved(c) for c in cands))


def _lv(local_vision):
    """local_vision=None 透传（元数据不可用三态）；其余归一 frozenset。"""
    return None if local_vision is None else frozenset(local_vision)


def _runtime(active=None, cloud=None, local_vision=()):
    return RuntimeView(
        active_services=set(active or []),
        cloud_models=dict(cloud or {}),
        local_vision=_lv(local_vision))


def _rt(*cands, active=None, cloud=None, local_vision=()):
    """自动就绪 runtime：cloud 候选注入发现列表，local 候选注入 active。
    显式 active/cloud 覆盖自动值（用于构造未就绪场景）。
    local_vision 不自动填充 — 视觉能力必须显式声明（7.9.0 语义）。"""
    auto_cloud = {f"{c.provider}/{c.model_id}": object()
                  for c in cands if c.kind == "cloud"}
    auto_active = {c.model_name for c in cands if c.kind == "local"}
    return RuntimeView(
        active_services=(set(active) if active is not None else auto_active),
        cloud_models={**auto_cloud, **(cloud or {})},
        local_vision=_lv(local_vision))


def _req(protocol="openai", vision=False):
    # 7.9.0：tools/stream/structured 不再构成路由要求（能力归模型，
    # 准入原则下恒真）；唯一请求感知维度 = vision
    return RequestRequirements(protocol=protocol, vision=vision)


class TestProtocolFilter:
    def test_openai_input_skips_anthropic_only_candidate(self):
        cands = [_cand("a1", 1, protocols=("anthropic",)),
                 _cand("a2", 2, protocols=("openai",))]
        snap = _pool_snap(cands)
        sel = select(snap, _req("openai"), _rt(*cands))
        assert sel.chosen.candidate.id == "a2"
        reasons = {d.candidate_id: d.reason for d in sel.decisions}
        assert reasons["a1"] == "no_evidence"

    def test_anthropic_input_skips_openai_only_candidate(self):
        cands = [_cand("a1", 1, protocols=("openai",)),
                 _cand("a2", 2, protocols=("anthropic",))]
        snap = _pool_snap(cands)
        sel = select(snap, _req("anthropic"), _rt(*cands))
        assert sel.chosen.candidate.id == "a2"
        assert {d.candidate_id: d.reason for d in sel.decisions}["a1"] == "no_evidence"

    def test_dual_protocol_candidate_chosen_for_both(self):
        cand = _cand("d1", 1, protocols=("openai", "anthropic"))
        snap = _pool_snap([cand])
        for proto in ("openai", "anthropic"):
            sel = select(snap, _req(proto), _rt(cand))
            assert sel.chosen.candidate.id == "d1"


class TestEvidenceRules:
    def test_declared_requires_pool_accept_declared(self):
        snap = _pool_snap([_cand("d1", 1, protocols=("openai",))],
                          accept_declared=False)
        # _resolved 默认 PROBED，这里手工构造 DECLARED
        cand = _cand("d1", 1, protocols=("openai",))
        snap = PoolSnapshot(
            revision=1, pool=snap.pool,
            candidates=(_resolved(cand, states=("DECLARED",)),))
        sel = select(snap, _req(), _runtime())
        assert sel.chosen is None
        assert sel.decisions[0].reason == "no_evidence"

    def test_declared_accepted_when_pool_allows(self):
        cand = _cand("d1", 1, protocols=("openai",))
        pool = PoolConfig(name="fast", enabled=True, allowed_locations=("cloud",),
                          accept_declared=True, candidates=(cand,))
        snap = PoolSnapshot(revision=1, pool=pool,
                            candidates=(_resolved(cand, states=("DECLARED",)),))
        sel = select(snap, _req(), _rt(cand))
        assert sel.chosen.candidate.id == "d1"

    def test_stale_evidence_never_participates(self):
        cand = _cand("d1", 1, protocols=("openai",))
        pool = PoolConfig(name="fast", enabled=True, allowed_locations=("cloud",),
                          accept_declared=True, candidates=(cand,))
        snap = PoolSnapshot(revision=1, pool=pool,
                            candidates=(_resolved(cand, states=("STALE",)),))
        sel = select(snap, _req(), _runtime())
        assert sel.chosen is None
        assert sel.decisions[0].reason == "no_evidence"

    def test_unknown_protocol_not_assumed_compatible(self):
        cand = _cand("d1", 1, protocols=("openai",))  # 无 anthropic 声明
        snap = _pool_snap([cand])
        sel = select(snap, _req("anthropic"), _runtime())
        assert sel.chosen is None
        assert sel.decisions[0].reason == "no_evidence"


class TestVisionFiltering:
    """7.9.0：能力归模型，池归优先级。vision 是唯一请求感知维度 —
    vision-capable 判定源 = 模型本体（本地 RuntimeView.local_vision ←
    models.d model_type=vl；云 cloud_models[key].supports_vision=True）。
    池候选 features 字段退役：读时容忍，不构成任何路由行为。
    """

    def test_vision_request_routes_to_vision_capable_local(self):
        cand = _cand("l1", 1, kind="local", model_name="qwen-vl")
        snap = _pool_snap([cand])
        sel = select(snap, _req(vision=True),
                     _rt(cand, active={"qwen-vl"}, local_vision={"qwen-vl"}))
        assert sel.chosen.candidate.id == "l1"

    def test_vision_request_routes_to_cloud_supports_vision_true(self):
        from types import SimpleNamespace
        cand = _cand("c1", 1, kind="cloud", provider="prov", model_id="m1")
        snap = _pool_snap([cand])
        sel = select(snap, _req(vision=True),
                     _rt(cand, cloud={"prov/m1": SimpleNamespace(
                         supports_vision=True)}))
        assert sel.chosen.candidate.id == "c1"

    def test_vision_request_excludes_non_vision_by_priority_fallthrough(self):
        # 文本候选 p1、视觉候选 p2：带图请求越过 p1 落到 p2
        cands = [_cand("t1", 1, kind="local", model_name="qwen-txt"),
                 _cand("l2", 2, kind="local", model_name="qwen-vl")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(vision=True),
                     _rt(*cands, active={"qwen-txt", "qwen-vl"},
                         local_vision={"qwen-vl"}))
        assert sel.chosen.candidate.id == "l2"
        assert ({d.candidate_id: d.reason for d in sel.decisions}["t1"]
                == "feature_unsupported")

    def test_no_vision_request_pure_priority_vl_at_p1_serves_all(self):
        # 用户裁决：VL 排最前就全接 — 无图请求不跳过 VL
        cands = [_cand("l1", 1, kind="local", model_name="qwen-vl"),
                 _cand("t2", 2, kind="local", model_name="qwen-txt")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(),
                     _rt(*cands, active={"qwen-vl", "qwen-txt"},
                         local_vision={"qwen-vl"}))
        assert sel.chosen.candidate.id == "l1"

    def test_no_vision_request_vl_at_p2_not_chosen_but_eligible(self):
        # VL 排 p2：无图请求按优先级走 p1；p2 只是没被选，不是被排除
        cands = [_cand("t1", 1, kind="local", model_name="qwen-txt"),
                 _cand("l2", 2, kind="local", model_name="qwen-vl")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(),
                     _rt(*cands, active={"qwen-txt", "qwen-vl"},
                         local_vision={"qwen-vl"}))
        assert sel.chosen.candidate.id == "t1"
        assert ({d.candidate_id: (d.reason, d.included) for d in sel.decisions}
                ["l2"] == ("ok", True))
        assert sel.compatible_count == 2

    def test_vision_request_no_vision_capable_is_compatibility_error(self):
        cands = [_cand("t1", 1, kind="local", model_name="qwen-txt")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(vision=True),
                     _rt(*cands, active={"qwen-txt"}))
        assert sel.chosen is None
        assert sel.decisions[0].reason == "feature_unsupported"
        assert sel.error == "no_compatible_target"

    def test_vision_unsupported_detail_is_self_explaining(self):
        cands = [_cand("t1", 1, kind="local", model_name="qwen-txt")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(vision=True),
                     _rt(*cands, active={"qwen-txt"}))
        d = sel.decisions[0]
        assert d.reason == "feature_unsupported"
        assert "vision" in d.detail and "not vision-capable" in d.detail

    def test_cloud_supports_vision_unset_is_not_vision_capable(self):
        # 未声明 supports_vision（None）≠ 能看图 — vision 必须显式声明
        from types import SimpleNamespace
        cand = _cand("c1", 1, kind="cloud", provider="prov", model_id="m1")
        snap = _pool_snap([cand])
        sel = select(snap, _req(vision=True),
                     _rt(cand, cloud={"prov/m1": SimpleNamespace(
                         supports_vision=None)}))
        assert sel.chosen is None
        assert sel.decisions[0].reason == "feature_unsupported"

    def test_undiscovered_cloud_candidate_not_statically_vision_excluded(self):
        # 云模型未发现 → 视觉能力未知，不做静态视觉判定；
        # 由就绪层报 cloud_model_missing（理由精确，不误导为能力问题）
        cand = _cand("c1", 1, kind="cloud", provider="prov", model_id="m1")
        snap = _pool_snap([cand])
        sel = select(snap, _req(vision=True), _runtime(cloud={}))
        assert sel.decisions[0].reason == "cloud_model_missing"

    def test_local_vision_metadata_unavailable_not_statically_excluded(self):
        # 安全评审（fail-open 路由）：local_vision=None = 元数据源不可用，
        # ≠ 「确认无本地模型能看图」。未知 → 不做静态视觉判定（同云未发现
        # 语义），按优先级路由 — 否则 allow_cross_location 下带图请求会被
        # 元数据故障改道到云
        cands = [_cand("t1", 1, kind="local", model_name="qwen-txt")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(vision=True),
                     _rt(*cands, active={"qwen-txt"}, local_vision=None))
        assert sel.chosen.candidate.id == "t1"
        assert ({d.candidate_id: (d.reason, d.included) for d in sel.decisions}
                ["t1"] == ("ok", True))

    def test_pool_candidate_features_field_is_ignored(self):
        # 存量 YAML 的候选 features（如遗留 [vision]）读时容忍、
        # 不构成任何路由行为：声明 [vision] 但模型本体无视觉 → 带图仍拒；
        # 未声明但模型本体有视觉 → 带图照接
        c1 = _cand("legacy", 1, kind="local", model_name="qwen-txt",
                   features=("vision",))
        snap = _pool_snap([c1])
        sel = select(snap, _req(vision=True),
                     _rt(c1, active={"qwen-txt"}, local_vision={"qwen-vl"}))
        assert sel.chosen is None
        c2 = _cand("plain", 1, kind="local", model_name="qwen-vl", features=())
        snap2 = _pool_snap([c2])
        sel2 = select(snap2, _req(vision=True),
                      _rt(c2, active={"qwen-vl"}, local_vision={"qwen-vl"}))
        assert sel2.chosen.candidate.id == "plain"

    def test_vision_excluded_counts_as_no_ready_not(self):
        # 视觉排除是静态兼容性问题（no_compatible_target），即使其他
        # 候选因未就绪被排除 — 与 7.8.0 错误码映射一致
        cands = [_cand("t1", 1, kind="local", model_name="qwen-txt"),
                 _cand("l2", 2, kind="local", model_name="qwen-vl")]
        snap = _pool_snap(cands)
        sel = select(snap, _req(vision=True),
                     _rt(*cands, active=set(), local_vision={"qwen-vl"}))
        # l2 视觉兼容但未就绪 → no_ready_target（不是 no_compatible）
        assert sel.error == "no_ready_target"


class TestLocationAndReadiness:
    def test_location_not_allowed_excludes(self):
        snap = _pool_snap([_cand("c1", 1, kind="cloud")], allowed=("local",))
        sel = select(snap, _req(), _runtime())
        assert sel.chosen is None
        assert sel.decisions[0].reason == "location_not_allowed"

    def test_local_candidate_not_active_is_excluded_not_error(self):
        snap = _pool_snap([_cand("l1", 1, kind="local", model_name="qwen")])
        sel = select(snap, _req(), _runtime(active=set()))  # 未激活
        assert sel.chosen is None
        assert sel.decisions[0].reason == "local_not_ready"
        assert sel.error == "no_ready_target"

    def test_local_candidate_active_is_chosen(self):
        snap = _pool_snap([_cand("l1", 1, kind="local", model_name="qwen")])
        sel = select(snap, _req(), _rt(active={"qwen"}))
        assert sel.chosen.candidate.id == "l1"

    def test_cloud_model_missing_in_discovery_excluded(self):
        snap = _pool_snap([_cand("c1", 1, kind="cloud", provider="prov", model_id="m1")])
        sel = select(snap, _req(), _runtime(cloud={}))  # 未发现
        assert sel.decisions[0].reason == "cloud_model_missing"

    def test_cloud_model_present_is_chosen(self):
        snap = _pool_snap([_cand("c1", 1, kind="cloud", provider="prov", model_id="m1")])
        sel = select(snap, _req(), _runtime(cloud={"prov/m1": object()}))
        assert sel.chosen.candidate.id == "c1"


class TestOrderingAndErrors:
    def test_priority_ascending_deterministic(self):
        cands = [_cand("p20", 20), _cand("p10", 10), _cand("p30", 30)]
        snap = _pool_snap(cands)
        sel = select(snap, _req(), _rt(*cands))
        assert sel.chosen.candidate.id == "p10"
        assert [d.candidate_id for d in sel.decisions] == ["p10", "p20", "p30"]

    def test_disabled_pool_error(self):
        snap = _pool_snap([_cand("c1", 1)], enabled=False)
        sel = select(snap, _req(), _runtime())
        assert sel.chosen is None
        assert sel.error == "pool_disabled"

    def test_no_compatible_target_when_all_protocol_excluded(self):
        snap = _pool_snap([_cand("a1", 1, protocols=("anthropic",))])
        sel = select(snap, _req("openai"), _runtime())
        assert sel.error == "no_compatible_target"

    def test_falls_through_to_next_when_first_not_ready(self):
        # 跨位置回退需显式开启（终审 #2：本地未就绪时绝不静默落云）
        snap = _pool_snap([
            _cand("l1", 1, kind="local", model_name="notactive"),
            _cand("c1", 2, kind="cloud", provider="prov", model_id="m1"),
        ], cross=True)
        sel = select(snap, _req(), _runtime(active=set(), cloud={"prov/m1": object()}))
        assert sel.chosen.candidate.id == "c1"

    def test_dry_run_decisions_fully_explained(self):
        snap = _pool_snap([
            _cand("l1", 1, kind="local", model_name="notactive"),
            _cand("bad", 2, protocols=("anthropic",)),
            _cand("c1", 3, kind="cloud", provider="prov", model_id="m1"),
        ])
        sel = select(snap, _req(), _runtime(active=set(), cloud={"prov/m1": object()}))
        reasons = {d.candidate_id: (d.reason, d.included) for d in sel.decisions}
        assert reasons["l1"] == ("local_not_ready", False)
        assert reasons["bad"] == ("no_evidence", False)
        # cross 默认关闭：home=local（l1 静态兼容优先级最高），云候选
        # 在就绪过滤之前即被排除（终审 #2）
        assert reasons["c1"] == ("cross_location_disabled", False)
        assert sel.error == "no_ready_target"
