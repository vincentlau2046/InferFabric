"""model_pool.config — 池配置数据模型与静态校验（纯函数，无 I/O 副作用）。"""

from pathlib import Path

import pytest
import yaml

from inferfabric.model_pool.config import (
    CandidateConfig,
    PoolConfig,
    PoolsFile,
    load_pools_dict,
    load_pools_file,
)


def _raw_pool(**overrides):
    pool = {
        "enabled": True,
        "strategy": "priority_failover",
        "allowed_locations": ["cloud"],
        "accept_declared": False,
        "candidates": [
            {
                "id": "primary",
                "priority": 10,
                "kind": "cloud",
                "provider": "provider_a",
                "model_id": "model_a",
                "declared_protocols": ["openai"],
            },
        ],
    }
    pool.update(overrides)
    return pool


def _raw_file(pools=None):
    return {
        "schema_version": 1,
        "pools": pools if pools is not None else {"fast": _raw_pool()},
    }


class TestValidParse:
    def test_valid_config_parses_with_defaults(self):
        cfg, issues = load_pools_dict(_raw_file(), current_revision=0)

        assert issues == []
        assert isinstance(cfg, PoolsFile)
        assert cfg.schema_version == 1
        assert cfg.revision == 0
        assert set(cfg.pools) == {"fast"}

        pool = cfg.pools["fast"]
        assert isinstance(pool, PoolConfig)
        assert pool.enabled is True
        assert pool.strategy == "priority_failover"
        assert pool.allowed_locations == ("cloud",)
        assert pool.accept_declared is False
        # 未配置 → 保守默认
        assert pool.allow_cross_location is False
        assert pool.max_attempts == 2
        assert pool.deadline_ms == 60000

        cand = pool.candidates[0]
        assert isinstance(cand, CandidateConfig)
        assert cand.id == "primary"
        assert cand.priority == 10
        assert cand.kind == "cloud"
        assert cand.provider == "provider_a"
        assert cand.model_id == "model_a"
        assert cand.declared_protocols == ("openai",)
        assert cand.features == ()  # 默认仅纯文本

    def test_local_candidate_uses_model_name(self):
        raw = _raw_file(pools={
            "small": {
                "enabled": True,
                "allowed_locations": ["local"],
                "candidates": [
                    {"id": "loc1", "priority": 1, "kind": "local",
                     "model_name": "qwen-small", "declared_protocols": ["openai"]},
                ],
            },
        })
        cfg, issues = load_pools_dict(raw, current_revision=3)

        assert issues == []
        assert cfg.revision == 3
        cand = cfg.pools["small"].candidates[0]
        assert cand.kind == "local"
        assert cand.model_name == "qwen-small"
        assert cand.provider == ""
        assert cand.model_id == ""

    def test_features_parsed(self):
        raw = _raw_file()
        raw["pools"]["fast"]["candidates"][0]["features"] = ["tools", "stream"]
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert issues == []
        assert cfg.pools["fast"].candidates[0].features == ("tools", "stream")

    def test_empty_candidate_list_pool_defaults_disabled(self):
        raw = _raw_file(pools={"premium": {"candidates": []}})
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert issues == []
        assert cfg.pools["premium"].enabled is False


class TestValidationIssues:
    def test_wrong_schema_version(self):
        raw = _raw_file()
        raw["schema_version"] = 2
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("schema_version" in i for i in issues)

    def test_pool_name_with_slash_rejected(self):
        raw = _raw_file(pools={"fast/x": _raw_pool()})
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("fast/x" in i for i in issues)

    def test_duplicate_candidate_id(self):
        pool = _raw_pool()
        pool["candidates"].append(dict(pool["candidates"][0]))
        cfg, issues = load_pools_dict(_raw_file(pools={"fast": pool}), current_revision=0)

        assert cfg is None
        assert any("primary" in i and "id" in i for i in issues)

    def test_duplicate_priority(self):
        pool = _raw_pool()
        pool["candidates"].append({
            "id": "backup", "priority": 10, "kind": "cloud",
            "provider": "b", "model_id": "m",
        })
        cfg, issues = load_pools_dict(_raw_file(pools={"fast": pool}), current_revision=0)

        assert cfg is None
        assert any("priority" in i for i in issues)

    def test_local_missing_model_name(self):
        raw = _raw_file(pools={"fast": {
            "enabled": True, "allowed_locations": ["local"],
            "candidates": [{"id": "c1", "priority": 1, "kind": "local"}],
        }})
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("model_name" in i for i in issues)

    def test_cloud_missing_provider(self):
        raw = _raw_file(pools={"fast": {
            "enabled": True, "allowed_locations": ["cloud"],
            "candidates": [{"id": "c1", "priority": 1, "kind": "cloud",
                            "model_id": "m"}],
        }})
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("provider" in i for i in issues)

    def test_unknown_kind(self):
        raw = _raw_file()
        raw["pools"]["fast"]["candidates"][0]["kind"] = "quantum"
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("kind" in i for i in issues)

    def test_candidate_target_referencing_pool_namespace(self):
        # 池引用池（递归）禁止
        raw = _raw_file()
        raw["pools"]["fast"]["candidates"][0] = {
            "id": "c1", "priority": 1, "kind": "cloud",
            "provider": "p", "model_id": "iff/other",
        }
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("iff/" in i for i in issues)

    def test_cross_location_requires_both_locations(self):
        raw = _raw_file()
        raw["pools"]["fast"]["allow_cross_location"] = True
        raw["pools"]["fast"]["allowed_locations"] = ["cloud"]
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("allow_cross_location" in i for i in issues)


class TestLoadFile:
    def test_missing_file_returns_none_no_issues(self, tmp_path):
        cfg, issues = load_pools_file(tmp_path / "absent.yaml")
        assert cfg is None
        assert issues == []

    def test_broken_yaml_returns_issues(self, tmp_path):
        p = tmp_path / "model_pools.yaml"
        p.write_text("pools: [unclosed", encoding="utf-8")
        cfg, issues = load_pools_file(p)
        assert cfg is None
        assert issues

    def test_valid_file_roundtrip(self, tmp_path):
        p = tmp_path / "model_pools.yaml"
        p.write_text(yaml.safe_dump(_raw_file()), encoding="utf-8")
        cfg, issues = load_pools_file(p)
        assert issues == []
        assert "fast" in cfg.pools

    def test_non_dict_root(self, tmp_path):
        p = tmp_path / "model_pools.yaml"
        p.write_text("- just\n- a\n- list\n", encoding="utf-8")
        cfg, issues = load_pools_file(p)
        assert cfg is None
        assert issues
