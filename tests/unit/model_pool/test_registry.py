"""model_pool.registry — 不可变快照、revision、CAS 热更新、原子写盘。"""

import json
from pathlib import Path

import pytest
import yaml

from inferfabric.model_pool.config import load_pools_dict
from inferfabric.model_pool.evidence import Evidence, EvidenceState
from inferfabric.model_pool.registry import (
    PoolRegistry,
    RegistrySnapshot,
    RevisionConflict,
)


def _raw_config(revision=0):
    return {
        "schema_version": 1,
        "pools": {
            "fast": {
                "enabled": True,
                "allowed_locations": ["cloud", "local"],
                "candidates": [
                    {"id": "c1", "priority": 1, "kind": "cloud",
                     "provider": "prov_a", "model_id": "model_a",
                     "declared_protocols": ["openai"]},
                    {"id": "c2", "priority": 2, "kind": "local",
                     "model_name": "qwen-small", "declared_protocols": ["openai"]},
                ],
            },
        },
    }


def _make_registry(tmp_path, known_local=None, known_providers=None,
                   local_protocols=None):
    return PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "evidence.json",
        known_local=known_local if known_local is not None else {"qwen-small"},
        known_providers=known_providers if known_providers is not None else {"prov_a"},
        provider_base=lambda p: "https://base/v1" if p == "prov_a" else "",
        local_protocols=local_protocols,
    )


class TestLoadAndSnapshot:
    def test_load_from_disk_and_snapshot(self, tmp_path):
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump(_raw_config()), encoding="utf-8")
        reg = _make_registry(tmp_path)
        reg.load()

        snap = reg.snapshot()
        assert isinstance(snap, RegistrySnapshot)
        assert snap.revision == 1  # 首次磁盘加载 = revision 1
        assert "fast" in snap.pools
        pool_snap = snap.pools["fast"]
        assert len(pool_snap.candidates) == 2
        # 优先级升序
        assert [c.candidate.id for c in pool_snap.candidates] == ["c1", "c2"]

    def test_missing_config_file_empty_snapshot(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.load()
        snap = reg.snapshot()
        assert snap.revision == 0
        assert snap.pools == {}

    def test_broken_config_keeps_empty_and_does_not_raise(self, tmp_path):
        (tmp_path / "model_pools.yaml").write_text("pools: [unclosed", encoding="utf-8")
        reg = _make_registry(tmp_path)
        reg.load()  # 不崩
        assert reg.snapshot().revision == 0

    def test_snapshot_is_immutable_reference(self, tmp_path):
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump(_raw_config()), encoding="utf-8")
        reg = _make_registry(tmp_path)
        reg.load()
        s1 = reg.snapshot()
        # 更新后旧快照对象不被修改
        reg.update(_raw_config(), expected_revision=s1.revision)
        s2 = reg.snapshot()
        assert s1.revision == 1 and s2.revision == 2
        assert s1 is not s2


class TestCandidateResolution:
    def test_dangling_local_target_goes_to_issues_not_candidates(self, tmp_path):
        reg = _make_registry(tmp_path, known_local={"other-model"})  # qwen-small 不存在
        reg.update(_raw_config(), expected_revision=0)
        snap = reg.snapshot()
        pool = snap.pools["fast"]
        assert [c.candidate.id for c in pool.candidates] == ["c1"]
        assert any("qwen-small" in i for i in pool.issues)

    def test_dangling_provider_goes_to_issues(self, tmp_path):
        reg = _make_registry(tmp_path, known_providers=set())  # prov_a 不存在
        reg.update(_raw_config(), expected_revision=0)
        snap = reg.snapshot()
        pool = snap.pools["fast"]
        assert [c.candidate.id for c in pool.candidates] == ["c2"]
        assert any("prov_a" in i for i in pool.issues)

    def test_evidence_injected_into_resolved_candidate(self, tmp_path):
        # 预置 probed 证据（指纹匹配 c1 的端点）
        reg = _make_registry(tmp_path)
        from inferfabric.model_pool.evidence import endpoint_fingerprint
        fp = endpoint_fingerprint(
            "cloud", "prov_a",
            "openai=https://base/v1|anthropic=https://base/v1", "model_a")
        ev_key = "cloud:prov_a:model_a:openai"
        reg.evidence.record(ev_key, Evidence(
            state=EvidenceState.PROBED, protocol="openai", features=("tools",),
            fingerprint=fp, recorded_at="t", source="admin-probe"))
        reg.update(_raw_config(), expected_revision=0)

        snap = reg.snapshot()
        c1 = snap.pools["fast"].candidates[0]
        assert c1.evidence["openai"].state == EvidenceState.PROBED

    def test_startup_load_reads_evidence_from_disk(self, tmp_path):
        # 先写证据文件，再全新 registry 启动加载 → 证据应注入候选
        from inferfabric.model_pool.evidence import EvidenceStore, endpoint_fingerprint
        ev_store = EvidenceStore(tmp_path / "evidence.json")
        fp = endpoint_fingerprint(
            "cloud", "prov_a",
            "openai=https://base/v1|anthropic=https://base/v1", "model_a")
        ev_store.record("cloud:prov_a:model_a:openai", Evidence(
            state=EvidenceState.PROBED, protocol="openai", features=(),
            fingerprint=fp, recorded_at="t", source="admin-probe"))
        ev_store.save()
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump(_raw_config()), encoding="utf-8")

        reg = _make_registry(tmp_path)
        reg.load()

        c1 = reg.snapshot().pools["fast"].candidates[0]
        assert c1.evidence["openai"].state == EvidenceState.PROBED

    def test_declared_candidate_gets_declared_evidence(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        snap = reg.snapshot()
        c2 = snap.pools["fast"].candidates[1]  # local qwen-small
        assert c2.evidence["openai"].state == EvidenceState.DECLARED
        # 未声明的协议 → UNKNOWN
        assert c2.evidence.get("anthropic", Evidence(EvidenceState.UNKNOWN, "", (), "", "", "")).state == EvidenceState.UNKNOWN

    def test_local_declared_rederived_from_live_engine_map(self, tmp_path):
        # 池 bug #1：目录修正（ninfer → 双协议）后，存量配置里保存的
        # declared_protocols（建池时只存了 ["openai"]）不得钉死证据 —
        # local 候选的 DECLARED 基线按引擎实时映射推导（local_protocols
        # 回调，proxy_manager 接 models.d 引擎类型），存量配置免迁移自愈。
        reg = _make_registry(
            tmp_path, local_protocols=lambda n: ["openai", "anthropic"])
        reg.update(_raw_config(), expected_revision=0)
        c2 = reg.snapshot().pools["fast"].candidates[1]  # 存盘只声明 openai
        assert c2.evidence["openai"].state == EvidenceState.DECLARED
        assert c2.evidence["anthropic"].state == EvidenceState.DECLARED

    def test_local_rederive_store_override_still_wins(self, tmp_path):
        # 自愈只重铸 DECLARED 基线：store 里的实测/失败条目（指纹匹配）
        # 仍覆盖声明 — 引擎实测不支持 anthropic 时不会被自愈翻案
        from inferfabric.model_pool.evidence import endpoint_fingerprint
        reg = _make_registry(
            tmp_path, local_protocols=lambda n: ["openai", "anthropic"])
        fp = endpoint_fingerprint("local", "qwen-small", "", "qwen-small")
        reg.evidence.record("local:qwen-small:qwen-small:anthropic", Evidence(
            state=EvidenceState.FAILED, protocol="anthropic", features=(),
            fingerprint=fp, recorded_at="t", source="admin-probe"))
        reg.update(_raw_config(), expected_revision=0)
        c2 = reg.snapshot().pools["fast"].candidates[1]
        assert c2.evidence["anthropic"].state == EvidenceState.FAILED
        assert c2.evidence["openai"].state == EvidenceState.DECLARED

    def test_local_rederive_narrows_stale_dual_declaration(self, tmp_path):
        # 反向自愈：引擎实时映射收窄时（如 vllm 换 ollama），存盘的多余
        # 声明不再铸造 DECLARED 证据 — 映射是声明的唯一权威源
        reg = _make_registry(tmp_path, local_protocols=lambda n: ["openai"])
        raw = _raw_config()
        raw["pools"]["fast"]["candidates"][1][
            "declared_protocols"] = ["openai", "anthropic"]
        reg.update(raw, expected_revision=0)
        c2 = reg.snapshot().pools["fast"].candidates[1]
        assert set(c2.evidence) == {"openai"}
        assert c2.evidence["openai"].state == EvidenceState.DECLARED


class TestCASUpdate:
    def test_update_success_increments_revision_and_persists(self, tmp_path):
        reg = _make_registry(tmp_path)
        snap = reg.update(_raw_config(), expected_revision=0)
        assert snap.revision == 1

        # 磁盘已写
        on_disk = yaml.safe_load((tmp_path / "model_pools.yaml").read_text(encoding="utf-8"))
        assert "fast" in on_disk["pools"]

        # 内存与磁盘一致（重建 registry 拿到同样内容）
        reg2 = _make_registry(tmp_path)
        reg2.load()
        assert reg2.snapshot().revision == 1
        assert "fast" in reg2.snapshot().pools

    def test_update_with_stale_revision_conflicts(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        with pytest.raises(RevisionConflict):
            reg.update(_raw_config(), expected_revision=0)  # 旧 revision
        # 磁盘未变，revision 仍 1
        assert reg.snapshot().revision == 1

    def test_update_with_invalid_config_raises_and_keeps_old(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        bad = _raw_config()
        bad["schema_version"] = 99
        with pytest.raises(ValueError):
            reg.update(bad, expected_revision=1)
        assert reg.snapshot().revision == 1
        assert "fast" in reg.snapshot().pools

    def test_update_is_atomic_no_partial_file(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        leftovers = [p for p in tmp_path.iterdir()
                     if p.name not in ("model_pools.yaml", "evidence.json")]
        assert leftovers == []

    def test_reload_from_disk_picks_up_external_edit(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        # 外部编辑：加一个池
        raw = _raw_config()
        raw["pools"]["premium"] = {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "p1", "priority": 1, "kind": "cloud",
                            "provider": "prov_a", "model_id": "model_x",
                            "declared_protocols": ["anthropic"]}],
        }
        (tmp_path / "model_pools.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert reg.reload_from_disk() is True
        snap = reg.snapshot()
        assert set(snap.pools) == {"fast", "premium"}
        assert snap.revision == 2  # 外部修改也是新 revision

    def test_reload_from_disk_broken_keeps_old(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_raw_config(), expected_revision=0)
        (tmp_path / "model_pools.yaml").write_text("not: [valid", encoding="utf-8")
        assert reg.reload_from_disk() is False
        assert "fast" in reg.snapshot().pools
        assert reg.snapshot().revision == 1
