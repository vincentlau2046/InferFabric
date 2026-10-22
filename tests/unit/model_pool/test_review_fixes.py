"""评审修复回归（2026-10-23 全分支评审，Important 发现逐条 TDD）。

编号对应评审报告：#1 配置类型硬化 / #2 外部编辑 revision / #3 证据指纹 /
#4 证据功能覆盖 / #5 不可迁移历史守卫 / #6 allow_cross_location 强制 /
#7 deadline 覆盖排队 / #9 提交后异常 / #10 客户端错误脱敏 / #11 错误体读取
守护 / #12 501 不回退 / #14 云发现 / #15 served_name 就绪 / #16 物理目标
限流与副本释放 / #18 attempt 级记录 / #20 SIGHUP 池重载 / #21 池不写缓存 /
#22 toggle CAS 顺序 / #23 竞态窗口 AttributeError。
"""

import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError as _UrllibHTTPError

import pytest
import yaml

import inferfabric.model_pool.dispatch as dispatch
from inferfabric.model_pool.config import load_pools_dict
from inferfabric.model_pool.evidence import Evidence, EvidenceState
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    CooldownStore,
    run_attempts,
)
from inferfabric.model_pool.registry import PoolRegistry, RevisionConflict
from inferfabric.model_pool.selector import (
    RequestRequirements,
    RuntimeView,
    select,
)

from tests.unit.model_pool.test_dispatch_openai import (
    FakeAuth,
    FakeGate,
    FakeHandler,
    FakePM,
    _local_model,
    _raw_config,
)


def _make_registry(tmp_path, known_local=None, known_providers=None,
                   provider_base=None):
    """与 test_dispatch_openai._raw_config 匹配的 registry 工厂（prov/qwen-small）。"""
    return PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "evidence.json",
        known_local=known_local if known_local is not None else {"qwen-small"},
        known_providers=known_providers if known_providers is not None else {"prov"},
        provider_base=provider_base or (lambda p: "https://base/v1"),
    )


def _cand(cid):
    from inferfabric.model_pool.config import CandidateConfig
    from inferfabric.model_pool.registry import ResolvedCandidate
    return ResolvedCandidate(
        candidate=CandidateConfig(id=cid, priority=1, kind="cloud",
                                  provider="prov", model_id="m",
                                  declared_protocols=("openai",)),
        endpoint_fingerprint=f"fp-{cid}", evidence={})


def _pool(max_attempts=3, deadline_ms=60000, **kw):
    return SimpleNamespace(max_attempts=max_attempts, deadline_ms=deadline_ms, **kw)


class Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cand):
        self.calls.append(cand.candidate.id)
        return self.results.pop(0) if self.results else AttemptResult()


# ── #1 配置类型硬化：畸形但可解析的 YAML 不抛 TypeError ──

class TestConfigTypeHardening:
    @pytest.mark.parametrize("bad", [
        {"id": "a", "priority": 1, "kind": [], "provider": "p", "model_id": "m"},
        {"id": ["a"], "priority": 1, "kind": "cloud", "provider": "p", "model_id": "m"},
        {"id": "a", "priority": 1, "kind": "cloud", "provider": "p", "model_id": "m",
         "declared_protocols": [{}]},
        {"id": "a", "priority": 1, "kind": "cloud", "provider": "p", "model_id": "m",
         "features": 4},
        {"id": "a", "priority": 1, "kind": "cloud", "provider": "p", "model_id": "m",
         "declared_protocols": "openai"},
    ])
    def test_malformed_types_return_issues_not_typeerror(self, bad):
        raw = {"schema_version": 1, "pools": {"fast": {
            "enabled": True, "candidates": [bad]}}}
        parsed, issues = load_pools_dict(raw, current_revision=0)
        assert parsed is None and issues, "malformed types must yield issues"

    def test_malformed_pool_level_types_return_issues(self):
        raw = {"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": "local", "candidates": []}}}
        parsed, issues = load_pools_dict(raw, current_revision=0)
        assert parsed is None and issues

    def test_registry_load_malformed_keeps_last_good(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        # 写入会让解析抛 TypeError 的畸形配置（若未硬化）
        (tmp_path / "model_pools.yaml").write_text(yaml.safe_dump(
            {"schema_version": 1, "revision": 99, "pools": {"fast": {
                "enabled": True, "candidates": [
                    {"id": "x", "priority": 1, "kind": [], "provider": "p",
                     "model_id": "m"}]}}}), encoding="utf-8")
        reg.reload_from_disk()   # 不得抛出
        snap = reg.snapshot()
        assert snap.revision == 1               # 保留最后有效快照
        assert {c.candidate.id for c in snap.pools["fast"].candidates} == {"loc", "cld"}


# ── #2 外部编辑保留 revision 键：内容变化必须推进 revision ──

class TestExternalEditRevision:
    def test_edit_in_place_with_same_revision_bumps(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        assert reg.snapshot().revision == 1
        # 外部编辑：新增池但保留 revision: 1（编辑器原地保存的常见形态）
        cfg = _raw_config()
        cfg["pools"]["extra"] = {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "e1", "priority": 1, "kind": "cloud",
                            "provider": "prov", "model_id": "cloud-m",
                            "declared_protocols": ["openai"]}]}
        cfg["revision"] = 1
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        assert reg.reload_from_disk() is True
        assert reg.snapshot().revision >= 2      # 内容变化 → revision 推进
        assert "extra" in reg.snapshot().pools

        # 持有过期 revision 1 的编辑者现在必须 409，不能再覆盖删除 extra
        with pytest.raises(RevisionConflict):
            reg.update(_raw_config(), expected_revision=1)

    def test_unchanged_content_reload_keeps_revision(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        reg.reload_from_disk()                    # 无编辑的重载
        assert reg.snapshot().revision == 1       # 不推进


# ── #3 依赖指纹变化 → 旧证据不注入 ──

class TestEvidenceFingerprint:
    def test_fingerprint_mismatch_excludes_stored_evidence(self, tmp_path):
        reg = _make_registry(tmp_path, known_local=set(),
                             known_providers={"prov"})
        # 写入配置（无声明协议）→ 记录 PROBED 证据（指纹=当前 base）
        cfg = {"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "c1", "priority": 1, "kind": "cloud",
                            "provider": "prov", "model_id": "m"}]}}}
        reg.update(cfg, expected_revision=0)
        key = "cloud:prov:m:openai"
        reg.evidence.record(key, Evidence(
            state=EvidenceState.PROBED, protocol="openai", features=("tools",),
            fingerprint=reg.snapshot().pools["fast"].candidates[0].endpoint_fingerprint,
            recorded_at="2026-10-23T00:00:00", source="admin-probe"))
        reg.evidence.save()                      # 持久化，供 reload/load 读取
        reg.reload_from_disk()
        assert "openai" in reg.snapshot().pools["fast"].candidates[0].evidence

        # 依赖变化：provider base URL 变了 → 旧 PROBED 证据不注入
        reg2 = PoolRegistry(
            config_path=tmp_path / "model_pools.yaml",
            evidence_path=tmp_path / "evidence.json",
            known_local=set(), known_providers={"prov"},
            provider_base=lambda p: "https://other/v1")
        reg2.load()
        ev = reg2.snapshot().pools["fast"].candidates[0].evidence
        assert "openai" not in ev    # 指纹不符 → 不参与选择


# ── #4 证据功能覆盖：PROBED(text-only) 不得放行 tools 请求 ──

class TestEvidenceFeatureCoverage:
    def _resolved(self, ev_features, cand_features=("tools",)):
        from inferfabric.model_pool.config import CandidateConfig
        from inferfabric.model_pool.registry import ResolvedCandidate
        return ResolvedCandidate(
            candidate=CandidateConfig(
                id="c1", priority=1, kind="cloud", provider="p", model_id="m",
                declared_protocols=("openai",), features=tuple(cand_features)),
            endpoint_fingerprint="fp",
            evidence={"openai": Evidence(
                state=EvidenceState.PROBED, protocol="openai",
                features=tuple(ev_features), fingerprint="fp",
                recorded_at="", source="admin-probe")})

    def test_probed_text_only_evidence_rejects_tools(self):
        from inferfabric.model_pool.config import PoolConfig
        from inferfabric.model_pool.registry import PoolSnapshot
        rc = self._resolved(ev_features=())            # 探测只确认了纯文本
        ps = PoolSnapshot(revision=1, pool=PoolConfig(
            name="fast", enabled=True, allowed_locations=("cloud",),
            accept_declared=False), candidates=(rc,))
        sel = select(ps, RequestRequirements(protocol="openai", tools=True),
                     RuntimeView(cloud_models={"p/m": object()}))
        assert sel.chosen is None
        assert sel.decisions[0].reason == "feature_unsupported"

    def test_probed_evidence_features_authorize(self):
        from inferfabric.model_pool.config import PoolConfig
        from inferfabric.model_pool.registry import PoolSnapshot
        rc = self._resolved(ev_features=("tools",))
        ps = PoolSnapshot(revision=1, pool=PoolConfig(
            name="fast", enabled=True, allowed_locations=("cloud",),
            accept_declared=False), candidates=(rc,))
        sel = select(ps, RequestRequirements(protocol="openai", tools=True),
                     RuntimeView(cloud_models={"p/m": object()}))
        assert sel.chosen is rc


# ── #5 不可迁移历史：无原目标归属 → 多候选池拒绝（spec §7）──

class TestNonMigratableHistory:
    def _registry(self, tmp_path, cfg=None):
        reg = _make_registry(tmp_path)
        reg.update(cfg or _raw_config(), expected_revision=0)
        return reg

    def test_openai_tool_history_rejected_in_multi_candidate_pool(self, tmp_path,
                                                                  monkeypatch):
        cfg = _raw_config()
        # 开启跨位置回退 → ordered 含两个真实目标（F6 默认关闭时只有一个，
        # 单目标无从迁移，工具历史可安全转发）
        cfg["pools"]["fast"]["allow_cross_location"] = True
        reg = self._registry(tmp_path, cfg)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        monkeypatch.setattr(dispatch, "_forward_request",
                            lambda *a, **kw: True)   # 无守卫 → 会被转发
        data = {"model": "iff/fast", "messages": [
            {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function",
             "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            {"role": "user", "content": "hi"}]}
        h = FakeHandler()
        assert dispatch.handle_pool_chat(h, pm, json.loads(json.dumps(data)),
                                         "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["status"] == "no_compatible_target"

    def test_anthropic_thinking_signature_rejected_in_multi_candidate_pool(
            self, tmp_path, monkeypatch):
        # 双候选均声明 anthropic（否则 400 来自协议过滤而非 §7 守卫）
        cfg = {"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": ["local", "cloud"],
            "accept_declared": True, "allow_cross_location": True,
            "candidates": [
                {"id": "loc", "priority": 1, "kind": "local",
                 "model_name": "qwen-small", "declared_protocols": ["anthropic"]},
                {"id": "cld", "priority": 2, "kind": "cloud",
                 "provider": "prov", "model_id": "cloud-m",
                 "declared_protocols": ["anthropic"]}]}}}
        reg = self._registry(tmp_path, cfg)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        monkeypatch.setattr(dispatch.forwarder, "forward_anthropic_local",
                            lambda *a, **kw: 200)   # 无守卫 → 会被转发
        data = {"model": "iff/fast", "max_tokens": 10, "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "sig-1"},
                {"type": "text", "text": "answer"}]},
            {"role": "user", "content": "hi"}]}
        h = FakeHandler()
        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["error"]["type"] == "invalid_request_error"

    def test_tool_history_allowed_in_single_candidate_pool(self, tmp_path,
                                                           monkeypatch):
        # 单候选：无从迁移 → 不拒绝（同一目标内重试安全）
        reg = self._registry(tmp_path)
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"] = [
            {"id": "loc", "priority": 1, "kind": "local",
             "model_name": "qwen-small", "declared_protocols": ["openai"]}]
        reg.update(cfg, expected_revision=reg.snapshot().revision)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})

        def fake_forward(handler, pmb, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None):
            return True
        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        data = {"model": "iff/fast", "messages": [
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            {"role": "user", "content": "hi"}]}
        h = FakeHandler()
        assert dispatch.handle_pool_chat(h, pm, json.loads(json.dumps(data)),
                                         "iff/fast") is True
        assert h.sent == []                    # 正常转发，无错误信封


# ── #6 allow_cross_location=False：跨位置候选不参与回退 ──

class TestCrossLocationEnforcement:
    def _pool_snap(self, allow_cross):
        from inferfabric.model_pool.config import CandidateConfig, PoolConfig
        from inferfabric.model_pool.registry import PoolSnapshot, ResolvedCandidate
        local = ResolvedCandidate(
            candidate=CandidateConfig(id="loc", priority=1, kind="local",
                                      model_name="qwen-small",
                                      declared_protocols=("openai",)),
            endpoint_fingerprint="fp-loc",
            evidence={"openai": Evidence(state=EvidenceState.DECLARED,
                                         protocol="openai", features=(),
                                         fingerprint="fp-loc", recorded_at="",
                                         source="admin-declare")})
        cloud = ResolvedCandidate(
            candidate=CandidateConfig(id="cld", priority=2, kind="cloud",
                                      provider="prov", model_id="m",
                                      declared_protocols=("openai",)),
            endpoint_fingerprint="fp-cld",
            evidence={"openai": Evidence(state=EvidenceState.DECLARED,
                                         protocol="openai", features=(),
                                         fingerprint="fp-cld", recorded_at="",
                                         source="admin-declare")})
        return PoolSnapshot(revision=1, pool=PoolConfig(
            name="fast", enabled=True, allowed_locations=("local", "cloud"),
            accept_declared=True, allow_cross_location=allow_cross),
            candidates=(local, cloud))

    def test_disabled_cross_location_restricts_ordered(self):
        sel = select(self._pool_snap(allow_cross=False),
                     RequestRequirements(protocol="openai"),
                     RuntimeView(active_services={"qwen-small"},
                                 cloud_models={"prov/m": object()}))
        assert sel.chosen is not None
        assert [rc.candidate.id for rc in sel.ordered] == ["loc"]
        reasons = {d.candidate_id: d.reason for d in sel.decisions}
        assert reasons["cld"] == "cross_location_disabled"

    def test_enabled_cross_location_keeps_both(self):
        sel = select(self._pool_snap(allow_cross=True),
                     RequestRequirements(protocol="openai"),
                     RuntimeView(active_services={"qwen-small"},
                                 cloud_models={"prov/m": object()}))
        assert [rc.candidate.id for rc in sel.ordered] == ["loc", "cld"]

    def test_local_conn_fail_does_not_fall_to_cloud_when_disabled(self):
        # 端到端：本地连接失败 + allow_cross_location=False → 不出云
        sel = select(self._pool_snap(allow_cross=False),
                     RequestRequirements(protocol="openai"),
                     RuntimeView(active_services={"qwen-small"},
                                 cloud_models={"prov/m": object()}))
        t = Recorder([AttemptResult(status=None, error="conn")])
        out = run_attempts(_pool(max_attempts=3), sel.ordered, t)
        assert out.ok is False
        assert t.calls == ["loc"]               # 云候选零调用


# ── #7 deadline 覆盖排队（gate 等待计入预算）──

class TestDeadlineCoversQueue:
    def test_deadline_start_before_gate(self):
        # deadline_start=0 → 循环开始时 mono()=100 已超 deadline → 零尝试
        t = Recorder([AttemptResult(status=None)])
        out = run_attempts(_pool(deadline_ms=1000), [_cand("c1")], t,
                           monotonic=lambda: 100.0, deadline_start=0.0)
        assert out.ok is False
        assert t.calls == []                    # 预算在排队中已耗尽


# ── #12 501 非回退：立即终止（不被 5xx 分支吞掉）──

class Test501NonFallback:
    def test_501_terminates_without_switch(self):
        t = Recorder([AttemptResult(status=501, error="not implemented"),
                      AttemptResult(ok=True)])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.ok is False and out.status == 501
        assert t.calls == ["c1"]                # 单次尝试，不重试不切换


# ── #18 attempt 级记录（spec §9.2）──

class TestAttemptRecords:
    def test_outcome_records_each_attempt(self):
        t = Recorder([AttemptResult(status=None, error="conn"),
                      AttemptResult(status=500, error="boom"),
                      AttemptResult(ok=True, status=200)])
        out = run_attempts(_pool(), [_cand("c1"), _cand("c2")], t)
        assert out.ok is True
        assert len(out.attempts) == 3
        assert out.attempts[0].candidate_id == "c1"
        assert out.attempts[0].status is None
        assert out.attempts[1].status == 500
        assert out.attempts[2].ok is True

    def test_dispatch_logs_failed_attempts_with_target_identity(self, monkeypatch):
        # dispatch 层：失败尝试各记一条 RequestLog，路由含物理目标标识
        reg = _make_registry_with_local()
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        monkeypatch.setattr(dispatch, "_forward_request",
                            lambda *a, **kw: False)   # 本地连接失败
        h = FakeHandler()
        dispatch.handle_pool_chat(h, pm, {"model": "iff/fast", "messages": []},
                                  "iff/fast")
        attempt_routes = [getattr(e, "route", None) for e in pm.logger.entries]
        assert any(r and r.startswith("pool:fast/local:qwen-small")
                   for r in attempt_routes)


# ── #15 served_name 就绪匹配 / #16 物理目标限流与副本释放 / #23 竞态窗口 ──

class TestLocalTargetIdentity:
    def test_runtime_view_uses_served_names(self):
        # active_services 存 ModelConfig.name；候选引用 served_name
        model = _local_model(name="muse-glimmer-vl", served="muse-glimmer")
        mgr = SimpleNamespace(active_services={"muse-glimmer-vl"})
        mgr.get_model = lambda n: model if n == "muse-glimmer-vl" else None
        pm = SimpleNamespace(mgr=mgr, cloud=SimpleNamespace(cloud_models={}),
                             pool_cooldowns=None)
        view = dispatch.runtime_view(pm)
        assert "muse-glimmer" in view.active_services

    def test_race_guard_returns_result_not_attributeerror(self):
        # select 后模型停止：ResolvedCandidate 无 model_name 属性 → 必须优雅返回
        from inferfabric.model_pool.config import CandidateConfig
        from inferfabric.model_pool.registry import ResolvedCandidate
        cand = ResolvedCandidate(
            candidate=CandidateConfig(id="loc", priority=1, kind="local",
                                      model_name="qwen-small",
                                      declared_protocols=("openai",)),
            endpoint_fingerprint="fp", evidence={})
        fake_pm = SimpleNamespace(
            mgr=SimpleNamespace(
                active_services=set(),
                find_model_by_served_name=lambda n: None))
        res = dispatch._attempt_local_openai(FakeHandler(), fake_pm,
                                             {"model": "iff/fast"}, model="iff/fast",
                                             pool="fast", cand=cand)
        assert res.status is None and res.committed is False
        assert "qwen-small" in res.error

    def test_forward_passes_real_model_name_for_replica_release(self, monkeypatch):
        # 副本记账：_forward_request 的 model_name 必须是真实模型名（释放配对）
        captured = {}

        def fake_forward(handler, pm, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None):
            captured["model_name"] = model_name
            return True

        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        reg = _make_registry_with_local()
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        dispatch._attempt_local_openai(
            FakeHandler(), pm, {"model": "iff/fast", "messages": []},
            model="iff/fast", pool="fast",
            cand=reg.snapshot().pools["fast"].candidates[0])
        assert captured["model_name"] == "qwen-small"


def _make_registry_with_local(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="iff-fix-"))
    reg = PoolRegistry(
        config_path=d / "m.yaml", evidence_path=d / "e.json",
        known_local={"qwen-small"}, known_providers=set())
    reg.update({"schema_version": 1, "pools": {"fast": {
        "enabled": True, "allowed_locations": ["local"],
        "accept_declared": True,
        "candidates": [{"id": "loc", "priority": 1, "kind": "local",
                        "model_name": "qwen-small",
                        "declared_protocols": ["openai"]}]}}},
        expected_revision=0)
    return reg


# ── #10 客户端错误脱敏：上游原始错误体不得回传客户端 ──

class TestClientErrorSanitization:
    def test_upstream_error_body_not_echoed_to_client(self):
        # outcome.error 含上游原始 body（可能带凭证）→ 信封用固定文案
        from inferfabric.model_pool.pool_forward import AttemptOutcome
        secret = "rejected credential sk-provider-SECRET"
        outcome = AttemptOutcome(ok=False, committed=False, status=400,
                                 error=f"Cloud provider error (400): {secret}")
        h = FakeHandler()
        pm = SimpleNamespace(logger=SimpleNamespace(log=lambda e: None))
        dispatch._finish_outcome_openai(
            h, pm, outcome, req_id="r", key_name="k", model="iff/fast")
        status, body, _ = h.sent[-1]
        assert status == 400
        assert "SECRET" not in json.dumps(body)
        assert body["status"] == "upstream_error"

    def test_pool_unavailable_message_has_no_upstream_detail(self):
        from inferfabric.model_pool.pool_forward import AttemptOutcome
        outcome = AttemptOutcome(
            ok=False, error="Cloud provider error (401): key sk-LEAKED")
        h = FakeHandler()
        pm = SimpleNamespace(logger=SimpleNamespace(log=lambda e: None))
        dispatch._finish_outcome_openai(
            h, pm, outcome, req_id="r", key_name="k", model="iff/fast")
        _, body, _ = h.sent[-1]
        assert "sk-LEAKED" not in json.dumps(body)

    def test_anthropic_envelope_sanitized(self):
        from inferfabric.model_pool.pool_forward import AttemptOutcome
        outcome = AttemptOutcome(ok=False, committed=False, status=400,
                                 error="Cloud provider error (400): sk-LEAK")
        h = FakeHandler()
        pm = SimpleNamespace(logger=SimpleNamespace(log=lambda e: None))
        dispatch._finish_outcome_anthropic(
            h, pm, outcome, req_id="r", key_name="k", model="iff/fast")
        _, body, _ = h.sent[-1]
        assert "sk-LEAK" not in json.dumps(body)
        assert body["type"] == "error"


# ── #14 池请求触发云发现（仅当池含云候选且 pm 提供该入口）──

class TestCloudDiscoveryOnPoolPath:
    def test_pool_request_calls_ensure_cloud_discovered(self, tmp_path, monkeypatch):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace()})
        pm.ensure_cloud_discovered_calls = 0

        def ecd():
            pm.ensure_cloud_discovered_calls += 1
        pm.ensure_cloud_discovered = ecd

        def fake_forward(handler, pmb, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None):
            return True
        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        h = FakeHandler()
        dispatch.handle_pool_chat(h, pm, {"model": "iff/fast", "messages": []},
                                  "iff/fast")
        assert pm.ensure_cloud_discovered_calls == 1

    def test_local_only_pool_skips_discovery(self, monkeypatch):
        reg = _make_registry_with_local()
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        pm.ensure_cloud_discovered_calls = 0
        pm.ensure_cloud_discovered = lambda: setattr(
            pm, "ensure_cloud_discovered_calls",
            pm.ensure_cloud_discovered_calls + 1)

        def fake_forward(handler, pmb, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None):
            return True
        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        h = FakeHandler()
        dispatch.handle_pool_chat(h, pm, {"model": "iff/fast", "messages": []},
                                  "iff/fast")
        assert pm.ensure_cloud_discovered_calls == 0


# ── #16 物理目标限流：attempt 级 acquire/release 真实目标键 ──

class TestPhysicalTargetGate:
    def test_attempt_acquires_physical_target_gate(self, monkeypatch):
        reg = _make_registry_with_local()
        gate = FakeGate()
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        pm.dual_gate = gate

        def fake_forward(handler, pmb, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None):
            return True
        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        dispatch._attempt_local_openai(
            FakeHandler(), pm, {"model": "iff/fast", "messages": []},
            model="iff/fast", pool="fast",
            cand=reg.snapshot().pools["fast"].candidates[0])
        assert gate.acquired == ["qwen-small"]      # 物理目标键
        assert gate.released == 1


# ── #22 toggle：CAS 先于存在性检查 ──

class TestToggleCasOrder:
    def test_stale_revision_on_deleted_pool_is_409(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)     # rev 1, pool fast
        # 外部删除 fast 池（内容变化 → rev 2）
        cfg = {"schema_version": 1, "pools": {}}
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump(cfg), encoding="utf-8")
        reg.reload_from_disk()
        assert "fast" not in reg.snapshot().pools
        with pytest.raises(RevisionConflict):
            # 过期 revision=1 + 已删除的池 → CAS 冲突（而非 404）
            reg.flip_pool_enabled("fast", False, expected_revision=1)


# ── #20 SIGHUP 全量重载包含池 registry ──

class TestReloaderIncludesPools:
    def test_reload_all_reloads_pool_registry_after_dependencies(self):
        from inferfabric.config_reloader import ConfigReloader
        order = []

        class FakePoolReg:
            def reload_from_disk(self):
                order.append("pools")

        class FakeMgr:
            def reload_models(self):
                order.append("models")

        rel = ConfigReloader(FakeMgr(), pool_registry=FakePoolReg())
        rel.reload_all()
        assert "pools" in order
        assert order.index("models") < order.index("pools")  # 依赖先于池


# ── forwarder 边界：#9 提交后异常 / #11 错误体读取守护 / #21 池不写缓存 ──

class _WFile:
    def __init__(self):
        self.data = b""

    def write(self, b):
        if isinstance(b, str):
            b = b.encode()
        self.data += b

    def flush(self):
        pass


class _FakeRespHandler:
    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = str(v)

    def end_headers(self):
        pass


def _provider_cfg():
    from inferfabric.cloud_discovery import CloudModel, ProviderConfig
    cfg = ProviderConfig(name="prov", api_key="sk-x",
                         openai_base="https://example.com/v1")
    cm = CloudModel(model_id="m", provider="prov",
                    openai_available=True, anthropic_available=True)
    return cfg, cm


class _TruncatedErrHTTPError(_UrllibHTTPError):
    """503 错误体读取中途断连（e.read() 抛 IncompleteRead）。"""

    def __init__(self):
        from io import BytesIO
        super().__init__("http://fake/", 503, "HTTP 503", {}, BytesIO(b""))

    def read(self, n=-1):
        from http.client import IncompleteRead
        raise IncompleteRead(b"", None)

    def close(self):
        pass


class TestForwarderBoundaryFixes:
    @patch("inferfabric.forwarder.urlopen")
    def test_truncated_error_body_yields_typed_result(self, mock_urlopen):
        # #11：上游 503 且错误体读取断连 → 分类结果（可回退），不是异常逃逸
        from inferfabric.forwarder import forward_to_cloud
        cfg, cm = _provider_cfg()
        mock_urlopen.side_effect = [_TruncatedErrHTTPError()]
        h = _FakeRespHandler()
        result = forward_to_cloud(h, {"model": "m", "messages": []}, cfg, cm,
                                  protocol="openai",
                                  suppress_client_errors=True)
        assert result.committed is False
        assert result.status == 503               # 按 5xx 动作表可回退
        assert h.status is None                   # 未写客户端

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_anthropic_local_json_write_failure_is_committed(self, mock_conn):
        # #9：非流式 JSON 响应已在写客户端时异常 → committed（不重放）
        from inferfabric.forwarder import forward_anthropic_local

        class _Resp:
            status = 200

            def getheader(self, h, default=None):
                return {"content-type": "application/json"}.get(h, default)

            def read(self, n=-1):
                return b'{"type":"message","content":[{"type":"text","text":"x"}]}'

            def close(self):
                pass

        conn = mock_conn.return_value
        conn.getresponse.return_value = _Resp()
        h = _FakeRespHandler()

        class _BoomFile(_WFile):
            def write(self, b):
                super().write(b)
                if b"}" in (b.encode() if isinstance(b, str) else b):
                    raise RuntimeError("client write exploded")

        h.wfile = _BoomFile()
        pm = SimpleNamespace(response_cache=None)
        model_obj = SimpleNamespace(name="m", served_name="s", port=8101)
        status = forward_anthropic_local(
            h, pm, {"model": "x", "messages": []},
            "key", model_obj, "iff/fast", suppress_client_errors=True)
        assert status == 200                       # committed 信号，非 None

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_anthropic_local_suppress_mode_skips_response_cache(self, mock_conn):
        # #21：池路径（suppress）不得写响应缓存（spec §9.3）
        from inferfabric.forwarder import forward_anthropic_local

        class _Resp:
            status = 200

            def getheader(self, h, default=None):
                return {"content-type": "application/json"}.get(h, default)

            def read(self, n=-1):
                return b'{"type":"message","content":[{"type":"text","text":"x"}],"usage":{"input_tokens":1,"output_tokens":1}}'

            def close(self):
                pass

        conn = mock_conn.return_value
        conn.getresponse.return_value = _Resp()
        h = _FakeRespHandler()
        cache_puts = []

        class _Cache:
            def put(self, *a, **kw):
                cache_puts.append(a)

        pm = SimpleNamespace(response_cache=_Cache())
        model_obj = SimpleNamespace(name="m", served_name="s", port=8101)
        forward_anthropic_local(
            h, pm, {"model": "x", "messages": []},
            "key", model_obj, "iff/fast", suppress_client_errors=True)
        assert cache_puts == []                    # 池路径零缓存写
