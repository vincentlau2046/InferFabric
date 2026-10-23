"""终审修复回归（2026-10-23 全分支终审，Important 发现逐条 TDD）。

编号对应终审报告：#1 PR-14 默认路径 / #2 跨位置开关绕过 / #3 不可迁移
历史可达唯一幸存备用 + redacted_thinking / #4 二次不变重载 revision 回滚 /
#5 快照路由 / #6 依赖指纹 / #7 deadline 传导 / #8 嵌套 gate 双计费 /
#10 Anthropic 本地传输契约 / #11 准入失败消耗预算 / #12 Retry-After 缩短 /
#13 Ollama 池分支 / #14 副本预留泄漏 / #15 attempt 日志污染与费用归属 /
#16 日志脱敏 / #17 冷却并发 / #18 半开单飞 + 404 隔离 / #19 重载失败报成功 /
#20 字符串布尔强转。
"""

import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

import inferfabric.model_pool.dispatch as dispatch
from inferfabric.model_pool.config import load_pools_dict
from inferfabric.model_pool.evidence import Evidence, EvidenceState
from inferfabric.forwarder import CloudResult
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    CooldownStore,
    run_attempts,
)
from inferfabric.model_pool.registry import PoolRegistry, ResolvedCandidate, RevisionConflict
from inferfabric.model_pool.selector import (
    RequestRequirements,
    RuntimeView,
    select,
)

from tests.unit.model_pool.test_dispatch_openai import (
    FakeAuth,
    FakeHandler,
    FakePM,
    _local_model,
    _raw_config,
)


def _make_registry(tmp_path, known_local=None, known_providers=None,
                   provider_base=None, provider_route=None):
    return PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "evidence.json",
        known_local=known_local if known_local is not None else {"qwen-small"},
        known_providers=known_providers if known_providers is not None else {"prov"},
        provider_base=provider_base or (lambda p: "https://base/v1"),
        provider_route=provider_route,
    )


def _pool(max_attempts=3, deadline_ms=60000, **kw):
    return SimpleNamespace(max_attempts=max_attempts, deadline_ms=deadline_ms, **kw)


# ── #20 字符串布尔不得静默开启安全开关 ──

class TestStrictBool:
    def test_quoted_false_string_rejected(self):
        raw = {
            "schema_version": 1,
            "pools": {"p": {
                "enabled": "false", "allowed_locations": ["local"],
                "accept_declared": "true", "allow_cross_location": "false",
                "candidates": [{"id": "a", "priority": 1, "kind": "local",
                                "model_name": "m", "declared_protocols": ["openai"]}],
            }},
        }
        parsed, issues = load_pools_dict(raw, current_revision=1)
        assert parsed is None
        assert any("enabled" in i and "boolean" in i for i in issues)
        assert any("accept_declared" in i for i in issues)
        assert any("allow_cross_location" in i for i in issues)

    def test_real_booleans_accepted(self):
        raw = {
            "schema_version": 1,
            "pools": {"p": {
                "enabled": True, "allowed_locations": ["local"],
                "accept_declared": False, "allow_cross_location": False,
                "candidates": [{"id": "a", "priority": 1, "kind": "local",
                                "model_name": "m", "declared_protocols": ["openai"]}],
            }},
        }
        parsed, issues = load_pools_dict(raw, current_revision=1)
        assert parsed is not None and not issues
        assert parsed.pools["p"].enabled is True


# ── #4 二次不变重载不得回滚 revision ──

class TestMonotonicRevision:
    def _write_external(self, tmp_path, pools, revision=1):
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "revision": revision,
                            "pools": pools}))

    def test_second_unchanged_reload_keeps_revision(self, tmp_path):
        reg = _make_registry(tmp_path)
        self._write_external(tmp_path, {"fast": {
            "enabled": True, "allowed_locations": ["local"],
            "candidates": [{"id": "a", "priority": 1, "kind": "local",
                            "model_name": "qwen-small",
                            "declared_protocols": ["openai"]}],
        }}, revision=1)
        assert reg.reload_from_disk() is True
        rev = reg.snapshot().revision
        # 二次重载（内容未变，磁盘 revision 键仍为 1）→ 不得回滚
        assert reg.reload_from_disk() is None
        assert reg.snapshot().revision == rev

    def test_stale_cas_rejected_after_external_pickup(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update({"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": ["local"],
            "candidates": [{"id": "a", "priority": 1, "kind": "local",
                            "model_name": "qwen-small",
                            "declared_protocols": ["openai"]}],
        }}}, expected_revision=0)   # → revision 1（文件写入 revision: 1）
        # 外部编辑：内容变化但原地保留 revision 键 1 → 拾取后 revision 2
        self._write_external(tmp_path, {"fast": {
            "enabled": True, "allowed_locations": ["local"],
            "candidates": [{"id": "a", "priority": 1, "kind": "local",
                            "model_name": "qwen-small",
                            "declared_protocols": ["openai"]},
                           {"id": "b", "priority": 2, "kind": "local",
                            "model_name": "qwen-small",
                            "declared_protocols": ["openai"]}],
        }}, revision=1)
        assert reg.reload_from_disk() is True
        assert reg.snapshot().revision == 2
        reg.reload_from_disk()   # 不变重载：revision 保持 2，不回滚到 1
        assert reg.snapshot().revision == 2
        # 持有 revision 1 的陈旧编辑不得覆盖外部新增的候选
        with pytest.raises(RevisionConflict):
            reg.update({"schema_version": 1, "pools": {}}, expected_revision=1)


# ── #19 重载结果三态：拒绝必须可区分 ──

class TestReloadTriState:
    def test_missing_file_is_none(self, tmp_path):
        reg = _make_registry(tmp_path)
        assert reg.reload_from_disk() is None

    def test_invalid_config_is_false(self, tmp_path):
        reg = _make_registry(tmp_path)
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "pools": {"bad": {
                "enabled": True, "allowed_locations": ["local"],
                "candidates": [{"id": "x", "priority": 1, "kind": "local",
                                # 缺 model_name → 校验失败
                                "declared_protocols": ["openai"]}],
            }}}))
        assert reg.reload_from_disk() is False

    def test_unchanged_content_is_none(self, tmp_path):
        reg = _make_registry(tmp_path)
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "revision": 3, "pools": {"fast": {
                "enabled": True, "allowed_locations": ["local"],
                "candidates": [{"id": "a", "priority": 1, "kind": "local",
                                "model_name": "qwen-small",
                                "declared_protocols": ["openai"]}],
            }}}))
        assert reg.reload_from_disk() is True
        assert reg.reload_from_disk() is None


# ── #5/#6 快照路由与双协议指纹 ──

class TestSnapshotRoute:
    def test_cloud_candidate_carries_both_bases(self, tmp_path):
        reg = _make_registry(
            tmp_path,
            provider_route=lambda p: {"openai_base": "https://o/v1",
                                      "anthropic_base": "https://a/v1"})
        reg.update({"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "c", "priority": 1, "kind": "cloud",
                            "provider": "prov", "model_id": "m",
                            "declared_protocols": ["openai"]}],
        }}}, expected_revision=0)
        cand = reg.snapshot().pools["fast"].candidates[0]
        assert cand.route["openai_base"] == "https://o/v1"
        assert cand.route["anthropic_base"] == "https://a/v1"

    def test_old_snapshot_keeps_old_route_after_provider_change(self, tmp_path):
        bases = {"prov": {"openai_base": "https://old/v1",
                          "anthropic_base": "https://old-a/v1"}}
        reg = _make_registry(tmp_path, provider_route=lambda p: bases[p])
        reg.update({"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "c", "priority": 1, "kind": "cloud",
                            "provider": "prov", "model_id": "m",
                            "declared_protocols": ["openai"]}],
        }}}, expected_revision=0)
        old_snap = reg.snapshot()
        bases["prov"] = {"openai_base": "https://new/v1",
                         "anthropic_base": "https://new-a/v1"}
        reg.reload_from_disk()
        # 旧快照引用不受影响（mid-flight 请求用编译时路由，终审 #5）
        assert old_snap.pools["fast"].candidates[0].route["openai_base"] == "https://old/v1"
        # 新快照反映新端点
        assert reg.snapshot().pools["fast"].candidates[0].route["openai_base"] == "https://new/v1"

    def test_fingerprint_changes_on_anthropic_base_only(self, tmp_path):
        bases = {"prov": {"openai_base": "https://o/v1",
                          "anthropic_base": "https://old-a/v1"}}
        reg = _make_registry(tmp_path, provider_route=lambda p: bases[p])
        reg.update({"schema_version": 1, "pools": {"fast": {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "c", "priority": 1, "kind": "cloud",
                            "provider": "prov", "model_id": "m",
                            "declared_protocols": ["openai", "anthropic"],
                            "features": ["tools"]}],
        }}}, expected_revision=0)
        fp_before = reg.snapshot().pools["fast"].candidates[0].endpoint_fingerprint
        # 注入一条旧指纹的 PROBED 证据（anthropic 协议）
        ev = Evidence(state=EvidenceState.PROBED, protocol="anthropic",
                      features=("tools",), fingerprint=fp_before,
                      recorded_at="", source="probe")
        reg.evidence.record("cloud:prov:m:anthropic", ev)
        reg.evidence.save()
        assert "anthropic" in reg.snapshot().pools["fast"].candidates[0].evidence
        # 只改 anthropic_base（openai_base 不变）→ 指纹变化，旧 PROBED 证据
        # 不为新端点背书（declared 证据按新指纹重新推导，属正常）（终审 #6）
        bases["prov"]["anthropic_base"] = "https://new-a/v1"
        reg.reload_from_disk()
        cand = reg.snapshot().pools["fast"].candidates[0]
        assert cand.endpoint_fingerprint != fp_before
        ev_anthropic = cand.evidence.get("anthropic")
        assert ev_anthropic is not None
        assert ev_anthropic.state != EvidenceState.PROBED
        assert ev_anthropic.fingerprint == cand.endpoint_fingerprint


# ── #2 跨位置开关在就绪过滤之前生效 ──

def _snap_with(pool_cfg, candidates):
    from inferfabric.model_pool.config import CandidateConfig, PoolConfig
    pool = PoolConfig(name="fast", **pool_cfg)
    from inferfabric.model_pool.registry import PoolSnapshot
    return PoolSnapshot(revision=1, pool=pool, candidates=tuple(candidates))


def _rc(cid, kind, priority, protocols=("openai",), features=(), model="m", provider="prov"):
    from inferfabric.model_pool.config import CandidateConfig
    return ResolvedCandidate(
        candidate=CandidateConfig(
            id=cid, priority=priority, kind=kind,
            model_name=model if kind == "local" else "",
            provider=provider if kind == "cloud" else "",
            model_id="cloud-m" if kind == "cloud" else "",
            declared_protocols=protocols, features=features),
        endpoint_fingerprint=f"fp-{cid}",
        evidence={p: Evidence(state=EvidenceState.DECLARED, protocol=p,
                              features=features, fingerprint=f"fp-{cid}",
                              recorded_at="", source="admin-declare")
                  for p in protocols},
    )


_REQ = RequestRequirements(protocol="openai")


class TestHomeLocation:
    def _pool_cfg(self, cross):
        return dict(enabled=True, allowed_locations=("local", "cloud"),
                    accept_declared=True, allow_cross_location=cross)

    def test_local_down_does_not_fall_to_cloud(self):
        snap = _snap_with(self._pool_cfg(cross=False), [
            _rc("loc", "local", 1), _rc("cld", "cloud", 2)])
        sel = select(snap, _REQ, RuntimeView(active_services=set(),
                                             cloud_models={"prov/cloud-m": object()}))
        assert sel.chosen is None
        assert sel.error == "no_ready_target"
        by_id = {d.candidate_id: d for d in sel.decisions}
        assert by_id["cld"].reason == "cross_location_disabled"
        assert by_id["loc"].reason == "local_not_ready"

    def test_local_cooling_does_not_fall_to_cloud(self):
        snap = _snap_with(self._pool_cfg(cross=False), [
            _rc("loc", "local", 1), _rc("cld", "cloud", 2)])
        sel = select(snap, _REQ,
                     RuntimeView(active_services={"m"},
                                 cloud_models={"prov/cloud-m": object()},
                                 cooling_fingerprints=frozenset({"fp-loc"})))
        assert sel.chosen is None
        by_id = {d.candidate_id: d for d in sel.decisions}
        assert by_id["cld"].reason == "cross_location_disabled"
        assert by_id["loc"].reason == "cooldown"

    def test_cross_enabled_falls_to_cloud(self):
        snap = _snap_with(self._pool_cfg(cross=True), [
            _rc("loc", "local", 1), _rc("cld", "cloud", 2)])
        sel = select(snap, _REQ, RuntimeView(active_services=set(),
                                             cloud_models={"prov/cloud-m": object()}))
        assert sel.chosen is not None and sel.chosen.candidate.id == "cld"

    def test_local_ready_excludes_cloud_from_failover_order(self):
        snap = _snap_with(self._pool_cfg(cross=False), [
            _rc("loc", "local", 1), _rc("cld", "cloud", 2)])
        sel = select(snap, _REQ, RuntimeView(active_services={"m"},
                                             cloud_models={"prov/cloud-m": object()}))
        assert sel.chosen.candidate.id == "loc"
        assert [c.candidate.id for c in sel.ordered] == ["loc"]


# ── #3 不可迁移历史：静态兼容数（而非当前就绪数）决定守卫 ──

class TestHistoryGuard:
    def test_compatible_count_counts_static_compatibility(self):
        snap = _snap_with(dict(enabled=True, allowed_locations=("local", "cloud"),
                               accept_declared=True, allow_cross_location=True), [
            _rc("loc", "local", 1), _rc("cld", "cloud", 2)])
        sel = select(snap, _REQ, RuntimeView(active_services=set(),
                                             cloud_models={"prov/cloud-m": object()}))
        # 本地未就绪 → ordered 只有 cloud，但静态兼容数 = 2
        assert len(sel.ordered) == 1
        assert sel.compatible_count == 2

    def test_single_target_pool_has_compatible_count_one(self):
        snap = _snap_with(dict(enabled=True, allowed_locations=("local",),
                               accept_declared=True, allow_cross_location=False), [
            _rc("loc", "local", 1)])
        sel = select(snap, _REQ, RuntimeView(active_services={"m"}))
        assert sel.compatible_count == 1

    def test_redacted_thinking_is_non_migratable(self):
        data = {"messages": [{"role": "user", "content": [
            {"type": "redacted_thinking", "data": "opaque"}]}]}
        assert dispatch._has_non_migratable_history("anthropic", data) is True

    def test_unsigned_thinking_is_migratable(self):
        data = {"messages": [{"role": "user", "content": [
            {"type": "thinking", "thinking": "hi"}]}]}
        assert dispatch._has_non_migratable_history("anthropic", data) is False


# ── #3 守卫用静态兼容数：冷却中的备用也是潜在迁移目标 ──

class TestHistoryGuardUsesCompatibleCount:
    def test_cooling_backup_still_triggers_guard(self, tmp_path, monkeypatch):
        import json as _json
        cfg = _raw_config()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        reg = _make_registry(tmp_path)
        reg.update(cfg, expected_revision=0)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        # 云备用在冷却 → ordered 只剩本地，但静态兼容数仍为 2：
        # 备用随时可能恢复，工具历史仍不得进入该池（终审 #3）
        fp = reg.snapshot().pools["fast"].candidates[1].endpoint_fingerprint
        pm.pool_cooldowns = CooldownStore()
        pm.pool_cooldowns.add(fp)
        monkeypatch.setattr(dispatch, "_forward_request", lambda *a, **kw: True)
        data = {"model": "iff/fast", "messages": [
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
            {"role": "user", "content": "hi"}]}
        h = FakeHandler()
        assert dispatch.handle_pool_chat(h, pm, _json.loads(_json.dumps(data)),
                                         "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["status"] == "no_compatible_target"


# ── #15/#16 dispatch 层日志（见 test_final_fixes_dispatch.py 分部）──

# ── Phase C：pool_forward（#7/#11/#12/#17/#18）──

from tests.unit.model_pool.test_pool_forward import (  # noqa: E402
    Recorder,
    _cand,
    _pool as _pf_pool,
)


class _FakeClock:
    """单调时钟：sleep 推进，可手动拨动。"""

    def __init__(self, start=0.0):
        self.t = float(start)
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class TestCooldownStoreFinal:
    # #17 并发安全
    def test_concurrent_operations_no_errors(self):
        store = CooldownStore(ttl_s=60)
        errors = []

        def worker(i):
            try:
                for _ in range(150):
                    fp = f"fp-{i % 4}"
                    store.add(fp)
                    store.is_cooling(fp)
                    store.cooling_fingerprints()
                    store.claim_probe(fp)
                    store.finish_probe(fp, True)
            except Exception as e:  # pragma: no cover - 仅收集
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    # #18 半开单飞
    def test_half_open_single_flight(self):
        store = CooldownStore(ttl_s=10)
        store.add("fp", now=0.0)
        assert not store.claim_probe("fp", now=5.0)    # 仍在冷却
        assert store.claim_probe("fp", now=11.0)       # 到期：首个获得探测权
        assert not store.claim_probe("fp", now=12.0)   # 并发第二个：拒绝（单飞）
        store.finish_probe("fp", ok=False, now=12.0)   # 探测失败 → 重新冷却
        assert store.is_cooling("fp", now=13.0)
        assert not store.claim_probe("fp", now=14.0)
        assert store.claim_probe("fp", now=25.0)       # 再次到期可再探
        store.finish_probe("fp", ok=True, now=26.0)    # 成功 → 完全恢复
        assert not store.is_cooling("fp", now=27.0)
        assert store.claim_probe("fp", now=28.0)       # 无冷却：正常放行

    def test_healthy_endpoint_claim_is_not_a_probe(self):
        store = CooldownStore(ttl_s=10)
        # 无冷却条目的端点：claim 放行但不注册探测
        assert store.claim_probe("absent", now=0.0)
        store.finish_probe("absent", ok=False, now=1.0)   # 无探测 → no-op
        assert not store.is_cooling("absent", now=2.0)

    def test_expired_entry_not_reported_but_probeable(self):
        store = CooldownStore(ttl_s=10)
        store.add("a", now=0.0)
        # 到期后不再上报给 selector（被动恢复语义保留）…
        assert store.cooling_fingerprints(now=15.0) == frozenset()
        # …但条目保留供半开探测（不得被扫描清除）
        assert store.claim_probe("a", now=15.0)

    # #18 404 隔离
    def test_quarantine_outlasts_normal_cooldown(self):
        store = CooldownStore(ttl_s=10)
        store.quarantine("fp", now=0.0)
        assert store.is_cooling("fp", now=100.0)      # 普通冷却 10s 早已过期
        assert not store.is_cooling("fp", now=2000.0)  # 隔离也有期限


class TestRunAttemptsFinal:
    # #7 deadline 传导：transport 收到剩余预算
    def test_transport_receives_remaining_budget(self):
        clock = _FakeClock(start=0.0)
        received = []

        def transport(cand, remaining):
            received.append(remaining)
            return AttemptResult(ok=True, status=200, committed=True)

        out = run_attempts(_pf_pool(deadline_ms=5000), [_cand("a")], transport,
                           monotonic=clock, sleep=clock.sleep)
        assert out.ok
        assert len(received) == 1
        assert 0 < received[0] <= 5.0

    def test_remaining_shrinks_across_wait(self):
        clock = _FakeClock(start=0.0)
        received = []

        def transport(cand, remaining):
            received.append((cand.candidate.id, remaining))
            if cand.candidate.id == "a":
                return AttemptResult(status=429, retry_after=2.0)
            return AttemptResult(ok=True, status=200, committed=True)

        out = run_attempts(_pf_pool(max_attempts=3, deadline_ms=10000),
                           [_cand("a"), _cand("b")], transport,
                           monotonic=clock, sleep=clock.sleep)
        assert out.ok
        # a：429 → 等待 2s 重试 → 再 429（已重试过）→ 切 b 成功
        assert [r[0] for r in received] == ["a", "a", "b"]
        assert received[2][1] < received[0][1]   # 等待消耗预算

    # #12 Retry-After 不得缩短：要求超过上限/剩余 → 切下一候选，绝不提前重试
    def test_retry_after_over_cap_advances_not_shortens(self):
        clock = _FakeClock(start=0.0)
        rec = Recorder([AttemptResult(status=429, retry_after=45.0),
                        AttemptResult(ok=True, status=200, committed=True)])

        out = run_attempts(_pf_pool(max_attempts=3, deadline_ms=60000),
                           [_cand("a"), _cand("b")], rec,
                           monotonic=clock, sleep=clock.sleep)
        assert out.ok
        assert rec.calls == ["a", "b"]     # a 不重试，直接切 b
        assert clock.sleeps == []          # 绝不按缩短后的等待提前重试

    def test_retry_after_within_budget_waits(self):
        clock = _FakeClock(start=0.0)
        rec = Recorder([AttemptResult(status=429, retry_after=5.0),
                        AttemptResult(ok=True, status=200, committed=True)])

        out = run_attempts(_pf_pool(max_attempts=3, deadline_ms=60000),
                           [_cand("a")], rec, monotonic=clock, sleep=clock.sleep)
        assert out.ok
        assert rec.calls == ["a", "a"]
        assert clock.sleeps == [5.0]       # 预算内尊重 Retry-After

    # #11 准入失败不消耗预算
    def test_admission_failure_does_not_consume_attempts(self):
        rec = Recorder([AttemptResult(admitted=False),
                        AttemptResult(ok=True, status=200, committed=True)])
        # max_attempts=1：若准入失败消耗预算，b 将无从尝试
        out = run_attempts(_pf_pool(max_attempts=1, deadline_ms=60000),
                           [_cand("a"), _cand("b")], rec)
        assert out.ok
        # a 的传输回调被调用但报告准入失败（不消耗预算）→ b 仍可尝试
        assert rec.calls == ["a", "b"]

    def test_all_admission_failures_pool_unavailable(self):
        rec = Recorder([AttemptResult(admitted=False),
                        AttemptResult(admitted=False)])
        out = run_attempts(_pf_pool(max_attempts=3, deadline_ms=60000),
                           [_cand("a"), _cand("b")], rec)
        assert not out.ok
        assert out.error_code == "pool_unavailable"
        # 准入失败不是真实上游调用 → 不产生 status 误导
        assert all(r.status is None for r in out.attempts)
        assert all(r.error == "not_admitted" for r in out.attempts)

    # #18 循环内准入：mid-flight 进入冷却的候选被跳过，不消耗预算
    def test_midflight_cooldown_skip_does_not_consume_budget(self):
        clock = _FakeClock(start=0.0)
        store = CooldownStore(ttl_s=60)
        store.add("fp-a", now=0.0)     # select 之后、attempt 之前被别的请求冷却
        rec = Recorder([AttemptResult(ok=True, status=200, committed=True)])
        out = run_attempts(_pf_pool(max_attempts=1, deadline_ms=60000),
                           [_cand("a"), _cand("b")], rec, cooldowns=store,
                           monotonic=clock, sleep=clock.sleep)
        assert out.ok
        assert rec.calls == ["b"]

    # #18 半开探测完成回写：探测失败 → 重新冷却；成功 → 恢复
    def test_probe_result_written_back(self):
        clock = _FakeClock(start=0.0)
        store = CooldownStore(ttl_s=10)
        store.add("fp-a", now=0.0)
        # 第一次：到期候选被探测，失败 → 重新冷却
        rec = Recorder([AttemptResult(status=500)])
        out = run_attempts(_pf_pool(max_attempts=1, deadline_ms=60000),
                           [_cand("a")], rec, cooldowns=store,
                           monotonic=clock, sleep=clock.sleep)
        assert not out.ok
        assert store.is_cooling("fp-a", now=clock.t)   # 重新冷却（新 TTL）

        # 第二次：到期 → 探测成功 → 完全恢复
        clock.t = 100.0
        rec2 = Recorder([AttemptResult(ok=True, status=200, committed=True)])
        out2 = run_attempts(_pf_pool(max_attempts=1, deadline_ms=60000),
                            [_cand("a")], rec2, cooldowns=store,
                            monotonic=clock, sleep=clock.sleep)
        assert out2.ok
        assert not store.is_cooling("fp-a", now=clock.t)
        assert store.claim_probe("fp-a", now=clock.t)  # 无条目：正常放行

    # #18 404 → 隔离（长 TTL）
    def test_404_quarantines_endpoint(self):
        store = CooldownStore(ttl_s=10)
        rec = Recorder([AttemptResult(status=404, error="not found")])
        out = run_attempts(_pf_pool(max_attempts=3, deadline_ms=60000),
                           [_cand("a"), _cand("b")], rec, cooldowns=store)
        assert not out.ok and out.status == 404       # 404 仍终止不回退
        assert store.is_cooling("fp-a", now=100.0)    # 隔离长于普通 TTL
        assert not store.is_cooling("fp-b", now=100.0)


# ── Phase D：dispatch（#8 acquire_target / #13 ollama / #14 端口预留 /
#    #15 日志归属 / #16 错误码 / #5 快照路由）──

class _FailTargetGate:
    """物理目标门恒失败（外层逻辑名门正常）。"""

    def __init__(self):
        self.acquired = []
        self.target_acquired = []
        self.released = 0

    def acquire(self, model, timeout=None):
        self.acquired.append(model)
        return SimpleNamespace(ok=True, reason="", release=self.release)

    def acquire_target(self, model, timeout=None):
        self.target_acquired.append(model)
        return SimpleNamespace(ok=False, reason="model_concurrency_limit:x",
                               release=self.release)

    def release(self):
        self.released += 1


class TestDispatchFinal:
    def _registry_cross(self, tmp_path, provider_route=None):
        cfg = _raw_config()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        reg = _make_registry(tmp_path, provider_route=provider_route)
        reg.update(cfg, expected_revision=0)
        return reg

    # #8：物理目标门走 acquire_target（不双扣 RPM/全局并发）
    def test_physical_gate_uses_acquire_target(self, tmp_path, monkeypatch):
        reg = self._registry_cross(tmp_path)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        monkeypatch.setattr(dispatch, "_forward_request", lambda *a, **kw: True)
        h = FakeHandler()
        assert dispatch.handle_pool_chat(
            h, pm, {"model": "iff/fast", "messages": []}, "iff/fast") is True
        assert pm.dual_gate.acquired == ["iff/fast"]          # 外层只一次全量 gate
        assert pm.dual_gate.target_acquired == ["qwen-small"]  # 物理门= acquire_target

    # #14：物理门失败 → 不预留副本端口
    def test_gate_failure_reserves_no_port(self, tmp_path, monkeypatch):
        reg = self._registry_cross(tmp_path)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        pm.dual_gate = _FailTargetGate()
        port_calls = []
        orig = pm.get_target_port
        pm.get_target_port = lambda m: (port_calls.append(m), orig(m))[1]
        monkeypatch.setattr(dispatch, "_forward_request",
                            lambda *a, **kw: True)   # 若被调用说明门失效
        h = FakeHandler()
        assert dispatch.handle_pool_chat(
            h, pm, {"model": "iff/fast", "messages": []}, "iff/fast") is True
        assert port_calls == []            # 门失败在端口预留之前
        assert h.sent and h.sent[0][0] in (400, 503)   # 明确失败信封

    # #13：ollama 本地候选走 OpenAI 兼容转发，不调 handle_ollama_native
    def test_ollama_candidate_uses_openai_compat(self, tmp_path, monkeypatch):
        reg = self._registry_cross(tmp_path)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model(type_="ollama")})
        native_calls = []
        monkeypatch.setattr(dispatch, "handle_ollama_native",
                            lambda *a, **kw: native_calls.append(1))
        forwarded = []

        def fake_forward(handler, pmb, target_port, body, stream, model_name="",
                         upstream_path=None, cache_body=None, timeout=None):
            forwarded.append(upstream_path)
            return True

        monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
        h = FakeHandler()
        assert dispatch.handle_pool_chat(
            h, pm, {"model": "iff/fast", "messages": []}, "iff/fast") is True
        assert native_calls == []                          # 原生分支已移除
        assert forwarded == ["/v1/chat/completions"]       # OpenAI 兼容端点

    # #15：失败 attempt 不写 RequestLog；最终成功行归属物理目标
    def test_attempt_failures_not_in_request_log(self, tmp_path, monkeypatch):
        reg = self._registry_cross(tmp_path)
        pm = FakePM(reg, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        name="prov", openai_base="https://live/v1",
                        anthropic_base="https://live-a/v1")})

        def fake_local(handler, pmb, target_port, body, stream, model_name="",
                       upstream_path=None, cache_body=None, timeout=None):
            return False   # 本地连接失败（未提交）

        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False,
                       timeout=None):
            cloud_calls.append(provider_cfg.openai_base)
            return SimpleNamespace(status=200, usage={"prompt_tokens": 1},
                                   ttft_ms=None, duration_ms=1.0, error=None,
                                   committed=True, retry_after=None)

        monkeypatch.setattr(dispatch, "_forward_request", fake_local)
        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        h = FakeHandler()
        assert dispatch.handle_pool_chat(
            h, pm, {"model": "iff/fast", "messages": []}, "iff/fast") is True
        # 恰好一条 RequestLog（最终成功）；attempt 失败不产生额外行
        assert len(pm.logger.entries) == 1
        row = pm.logger.entries[0]
        assert row.model == "cloud-m"            # 物理模型（费用归属，终审 #15）
        assert row.cloud_provider == "prov"
        assert row.route == "pool:fast/cloud:prov/cloud-m"

    # #5：云候选用编译期快照路由，不读 live provider 配置
    def test_cloud_uses_snapshot_route(self, tmp_path, monkeypatch):
        reg = self._registry_cross(
            tmp_path, provider_route=lambda p: {"openai_base": "https://snap/v1",
                                                "anthropic_base": "https://snap-a/v1"})
        pm = FakePM(reg, active=set(),   # 本地未就绪 → 云候选
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        name="prov", openai_base="https://live/v1",
                        anthropic_base="https://live-a/v1")})
        bases = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False,
                       timeout=None):
            bases.append(provider_cfg.openai_base)
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None,
                                   committed=True, retry_after=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        h = FakeHandler()
        assert dispatch.handle_pool_chat(
            h, pm, {"model": "iff/fast", "messages": []}, "iff/fast") is True
        assert bases == ["https://snap/v1"]   # 快照路由，非 live 配置

    # #16：RequestLog 错误字段只用固定码，不携带上游错误体
    def test_final_error_log_fixed_code(self, tmp_path, monkeypatch):
        reg = self._registry_cross(tmp_path)
        pm = FakePM(reg, active=set(),
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        name="prov", openai_base="https://live/v1",
                        anthropic_base="https://live-a/v1")})

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False,
                       timeout=None):
            # 已提交的失败：错误体含上游敏感内容
            return SimpleNamespace(status=500, usage={}, ttft_ms=None,
                                   duration_ms=1.0,
                                   error="Cloud provider error (500): SECRET-BODY",
                                   committed=True, retry_after=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        h = FakeHandler()
        assert dispatch.handle_pool_chat(
            h, pm, {"model": "iff/fast", "messages": []}, "iff/fast") is True
        row = pm.logger.entries[-1]
        assert "SECRET-BODY" not in (row.error or "")
        assert row.error == "upstream_http_500"


# ── Phase F 接线：#19 重载三态进 config_reloader / #5 provider_route 进 proxy_manager ──

class TestPhaseFWiring:
    def _reloader_with_pool(self, result):
        from inferfabric.config_reloader import ConfigReloader

        class FakePoolReg:
            def reload_from_disk(self):
                return result

        class FakeMgr:
            def reload_models(self):
                pass

        return ConfigReloader(FakeMgr(), pool_registry=FakePoolReg())

    def test_pool_reload_false_marks_domain_failed(self):
        # False = 文件损坏/不可读 → 报告失败域（终审 #19：不再假成功）
        failed = self._reloader_with_pool(False).reload_all()
        assert "model_pools" in failed

    def test_pool_reload_none_is_ok(self):
        # None = 无文件/未变化 → 正常
        failed = self._reloader_with_pool(None).reload_all()
        assert failed == []

    def test_pool_reload_true_is_ok(self):
        failed = self._reloader_with_pool(True).reload_all()
        assert failed == []

    def test_proxy_manager_provider_route_returns_both_bases(self):
        # 终审 #5/#6：指纹/路由快照必须覆盖双协议 base — proxy_manager 以
        # provider_route（而非单 base provider_base）装配 registry
        from inferfabric import proxy_manager as pm_mod
        cfg = SimpleNamespace(openai_base="https://o/v1",
                              anthropic_base="https://a/v1")

        class FakeCloud:
            def get_provider_config(self, name):
                return cfg if name == "prov" else None

        fake_pm = SimpleNamespace(cloud=FakeCloud())
        route = pm_mod.ProxyManager._provider_route_urls(fake_pm, "prov")
        assert route == {"openai_base": "https://o/v1",
                         "anthropic_base": "https://a/v1"}
        # 未配置的 provider → 双空串（不进指纹维度）
        assert pm_mod.ProxyManager._provider_route_urls(
            fake_pm, "missing") == {"openai_base": "", "anthropic_base": ""}
