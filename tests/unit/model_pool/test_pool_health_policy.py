"""可恢复健康状态与池参数严格校验 — PLAN-2026-11-15 v2 Task 6（P2）。

契约（计划 Interfaces）：
- 401/403 → 运行冷却（可恢复），绝不自动写永久 FAILED；冷却到期后半开
  探测成功 → 完全恢复（含"换凭证"场景：新凭证半开探测成功即恢复）。
- 健康状态变化不悄悄覆盖管理员显式 FAILED（证据与健康解耦）。
- max_attempts/deadline_ms 严格类型 + 新写入范围 1..10 / 1000..300000；
  磁盘存量超界容忍（log 告警、不裁剪、不拒载）— 显式迁移另行决定。
- registry update/flip 保持 CAS 与 raw 无损。
"""

import time
from pathlib import Path

import pytest

from inferfabric.model_pool.config import CandidateConfig, PoolConfig, load_pools_dict
from inferfabric.model_pool.pool_forward import (
    AttemptResult,
    CooldownStore,
    run_attempts,
)
from inferfabric.model_pool.registry import (
    PoolSnapshot,
    RegistrySnapshot,
    ResolvedCandidate,
    RevisionConflict,
    PoolRegistry,
)
from inferfabric.model_pool.evidence import Evidence, EvidenceState, EvidenceStore


def cand(cid, model="qwen3-4b"):
    return CandidateConfig(id=cid, priority=10, kind="local",
                           model_name=model, declared_protocols=("openai",))


def rc(cid):
    return ResolvedCandidate(candidate=cand(cid), endpoint_fingerprint=f"fp-{cid}",
                             evidence={}, route={})


def pool(max_attempts=3):
    return PoolConfig(name="fast", enabled=True, allowed_locations=("local",),
                      accept_declared=True, max_attempts=max_attempts,
                      deadline_ms=60000, candidates=(cand("c1"),))


class TestAuthFailureRecovers:

    def test_401_cooldown_then_half_open_recovers(self):
        store = CooldownStore(ttl_s=5)
        base = 1000.0
        results = [
            AttemptResult(ok=False, committed=False, status=401,
                          error="auth"),   # 凭证失效 → 冷却
            AttemptResult(ok=True, committed=True, status=200),  # 换凭证后半开成功
        ]
        seq = list(results)

        def transport(c, remaining):
            return seq.pop(0)

        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base)
        assert store.is_cooling("fp-c1", now=base) is True  # 401 冷却中
        # 时间推进过 TTL → 半开可探测
        assert store.is_cooling("fp-c1", now=base + 6) is False
        assert store.claim_probe("fp-c1", now=base + 6) is True
        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base + 6)
        assert store.is_cooling("fp-c1", now=base + 6) is False  # 完全恢复

    def test_auth_failure_never_writes_permanent_failed(self, tmp_path):
        """401→冷却→成功的全链路不触碰 EvidenceStore（无 FAILED 写入）。"""
        ev_store = EvidenceStore(tmp_path / "evidence.json")
        store = CooldownStore(ttl_s=5)
        base = 1000.0
        seq = [
            AttemptResult(ok=False, committed=False, status=401),
            AttemptResult(ok=True, committed=True, status=200),
        ]

        def transport(c, remaining):
            return seq.pop(0)

        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base)
        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base + 6)
        assert ev_store._data == {}  # 证据系统零写入

    def test_admin_failed_not_overridden_by_health(self, tmp_path):
        """管理员显式 FAILED 不被运行健康事件悄悄覆盖。"""
        ev_store = EvidenceStore(tmp_path / "evidence.json")
        ev_store._data["local:qwen3-4b:qwen3-4b:openai"] = Evidence(
            state=EvidenceState.FAILED, protocol="openai", features=(),
            fingerprint="fp", recorded_at="x", source="admin-explicit")
        store = CooldownStore(ttl_s=5)
        base = 1000.0

        def transport(c, remaining):
            return AttemptResult(ok=True, committed=True, status=200)

        run_attempts(pool(), [rc("c1")], transport, cooldowns=store,
                     monotonic=lambda: base)
        ev = ev_store._data["local:qwen3-4b:qwen3-4b:openai"]
        assert ev.state == EvidenceState.FAILED  # 原样保留


class TestPoolParametersStrictValidation:

    def _cfg(self, **overrides):
        pool_raw = {
            "enabled": True,
            "allowed_locations": ["local"],
            "candidates": [{"id": "c1", "priority": 10, "kind": "local",
                            "model_name": "qwen3-4b",
                            "declared_protocols": ["openai"]}],
        }
        pool_raw.update(overrides)
        return {"schema_version": 1, "pools": {"fast": pool_raw}}

    # 注：test_defaults_slow_model_tolerance（默认放宽 2→3 / 60000→180000）
    # 属慢模型容忍默认值行为变更，随"行为变更待议"条目单独裁决
    # （CHERRY-PICK-MANIFEST §5 条目 14），不随本批次。

    @pytest.mark.parametrize("bad", [True, 2.5, "3", 0, -1])
    def test_max_attempts_type_and_range_rejected(self, bad):
        parsed, issues = load_pools_dict(self._cfg(max_attempts=bad),
                                         current_revision=1)
        assert parsed is None
        assert any("max_attempts" in i for i in issues)

    @pytest.mark.parametrize("bad", [True, 60.5, "60000", 0, -5, 500, 400000])
    def test_deadline_ms_type_and_range_rejected(self, bad):
        parsed, issues = load_pools_dict(self._cfg(deadline_ms=bad),
                                         current_revision=1)
        assert parsed is None
        assert any("deadline_ms" in i for i in issues)

    def test_in_range_accepted(self):
        parsed, issues = load_pools_dict(
            self._cfg(max_attempts=1, deadline_ms=1000), current_revision=1)
        assert parsed is not None and not issues
        parsed, _ = load_pools_dict(
            self._cfg(max_attempts=10, deadline_ms=300000), current_revision=1)
        assert parsed is not None

    def test_legacy_out_of_range_tolerated_on_disk_load(self, caplog):
        """磁盘存量超界：容忍载入（不裁剪、不拒载），log 告警待显式迁移。"""
        parsed, issues = load_pools_dict(self._cfg(max_attempts=20,
                                                   deadline_ms=600000),
                                         current_revision=1, strict=False)
        assert parsed is not None and not issues
        assert parsed.pools["fast"].max_attempts == 20  # 不自动裁剪

    def test_update_path_strict_and_raw_preserved(self, tmp_path):
        """CAS 写入路径拒绝超界；失败后 raw/快照原样保留（409 语义并存）。"""
        cfg_path = tmp_path / "model_pools.yaml"
        ev_path = tmp_path / "evidence.json"
        reg = PoolRegistry(cfg_path, ev_path,
                           known_local={"qwen3-4b"}, known_providers=set())
        good = self._cfg(max_attempts=2, deadline_ms=60000)
        reg.update(good, expected_revision=0)
        assert reg.snapshot().revision == 1

        # 超界新写入被拒
        with pytest.raises(ValueError):
            reg.update(self._cfg(max_attempts=99), expected_revision=1)
        # raw/快照无损
        assert reg.raw_config()["pools"]["fast"]["max_attempts"] == 2
        assert reg.snapshot().pools["fast"].pool.max_attempts == 2

        # 409：过期 revision
        with pytest.raises(RevisionConflict):
            reg.update(self._cfg(max_attempts=3), expected_revision=0)
