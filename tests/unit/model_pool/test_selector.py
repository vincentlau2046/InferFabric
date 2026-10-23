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


def _runtime(active=None, cloud=None):
    return RuntimeView(
        active_services=set(active or []),
        cloud_models=dict(cloud or {}))


def _rt(*cands, active=None, cloud=None):
    """自动就绪 runtime：cloud 候选注入发现列表，local 候选注入 active。
    显式 active/cloud 覆盖自动值（用于构造未就绪场景）。"""
    auto_cloud = {f"{c.provider}/{c.model_id}": object()
                  for c in cands if c.kind == "cloud"}
    auto_active = {c.model_name for c in cands if c.kind == "local"}
    return RuntimeView(
        active_services=(set(active) if active is not None else auto_active),
        cloud_models={**auto_cloud, **(cloud or {})})


def _req(protocol="openai", stream=False, tools=False, vision=False, structured=False):
    return RequestRequirements(protocol=protocol, stream=stream, tools=tools,
                               vision=vision, structured=structured)


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


class TestFeatures:
    def test_tools_request_excludes_text_only_candidate(self):
        cands = [_cand("t1", 1, features=()),   # 纯文本
                 _cand("t2", 2, features=("tools",))]
        snap = _pool_snap(cands)
        sel = select(snap, _req(tools=True), _rt(*cands))
        assert sel.chosen.candidate.id == "t2"
        assert {d.candidate_id: d.reason for d in sel.decisions}["t1"] == "feature_unsupported"

    def test_vision_request_excludes_non_vision(self):
        snap = _pool_snap([_cand("v1", 1, features=("tools",))])
        sel = select(snap, _req(vision=True), _runtime())
        assert sel.chosen is None
        assert sel.decisions[0].reason == "feature_unsupported"

    def test_stream_and_structured_flags_checked(self):
        cand = _cand("s1", 1, features=("stream",))
        snap = _pool_snap([cand])
        assert select(snap, _req(stream=True), _rt(cand)).chosen.candidate.id == "s1"
        assert select(snap, _req(structured=True), _runtime()).decisions[0].reason == "feature_unsupported"


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
