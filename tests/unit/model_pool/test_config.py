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

    def test_distinct_candidate_ids_across_pools_allowed(self):
        # 反向守护：跨池全局唯一只禁「同 id」，不同 id 跨池完全合法
        pools = {
            "fast": {
                "enabled": True, "allowed_locations": ["cloud"],
                "candidates": [{"id": "c1", "priority": 1, "kind": "cloud",
                                "provider": "a", "model_id": "m1",
                                "declared_protocols": ["openai"]}],
            },
            "premium": {
                "enabled": True, "allowed_locations": ["cloud"],
                "candidates": [{"id": "c2", "priority": 1, "kind": "cloud",
                                "provider": "b", "model_id": "m2",
                                "declared_protocols": ["openai"]}],
            },
        }
        cfg, issues = load_pools_dict(_raw_file(pools=pools), current_revision=0)

        assert issues == []
        assert set(cfg.pools) == {"fast", "premium"}


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
        # 池内重复只由 _parse_pool 报，不得误触发跨池「globally unique」消息
        # （若未来把跨池检查改成按出现次数而非按池集合键控，这里会守住边界）
        assert not any("globally unique" in i for i in issues)

    def test_candidate_id_must_be_unique_across_pools(self):
        # 候选 id 跨池全局唯一（内部不变量）：id 是决策/事件/日志引用的稳定标签，
        # 跨池重复会让按 id 的引用归属歧义；当前记录虽已按池限定，固定此规则
        # 防御未来按 id 键控的视图。池内重复由 _parse_pool 兜底，这里是跨池规则。
        pools = {
            "fast": {
                "enabled": True, "allowed_locations": ["cloud"],
                "candidates": [{"id": "shared", "priority": 1, "kind": "cloud",
                                "provider": "a", "model_id": "m1",
                                "declared_protocols": ["openai"]}],
            },
            "premium": {
                "enabled": True, "allowed_locations": ["cloud"],
                "candidates": [{"id": "shared", "priority": 1, "kind": "cloud",
                                "provider": "b", "model_id": "m2",
                                "declared_protocols": ["openai"]}],
            },
        }
        cfg, issues = load_pools_dict(_raw_file(pools=pools), current_revision=0)

        assert cfg is None
        assert any("shared" in i and "globally unique" in i for i in issues)
        # 报出涉事两池，便于定位
        assert any("fast" in i and "premium" in i for i in issues)

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
        # 池引用池（递归）禁止 — v2 命名空间硬切 fabric-（原 iff/）
        raw = _raw_file()
        raw["pools"]["fast"]["candidates"][0] = {
            "id": "c1", "priority": 1, "kind": "cloud",
            "provider": "p", "model_id": "fabric-flash",
        }
        cfg, issues = load_pools_dict(raw, current_revision=0)

        assert cfg is None
        assert any("fabric-" in i for i in issues)

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

    def test_priority_type_rejected(self):
        # 评审 PR#4 nit 3：priority 与 max_attempts 同标准 — bool/float/str
        # 经 _strict_int 一律拒绝（int(True)=1 静默畸变不再发生）
        for bad in (True, 2.5, "3"):
            raw = _raw_file()
            raw["pools"]["fast"]["candidates"][0]["priority"] = bad
            cfg, issues = load_pools_dict(raw, current_revision=0)
            assert cfg is None
            assert any("priority" in i for i in issues)


def _raw_pool_n_candidates(n, **overrides):
    raw = _raw_pool(**overrides)
    raw["candidates"] = [
        {"id": f"c{i}", "priority": i, "kind": "cloud",
         "provider": "prov", "model_id": f"m{i}",
         "declared_protocols": ["openai"]}
        for i in range(1, n + 1)
    ]
    return raw


def test_attempts_below_candidate_count_auto_extends_on_write():
    # 用户裁决 2026-11-24：修复不得绑定配置迁移。max_attempts < 候选数
    # （fabric-max 事故形态）不再拒绝——解析层自动扩展有效槽位到全覆盖
    # （= 候选数）；生产存量 YAML 零变更即生效，对齐降级为可选整理
    raw = {"schema_version": 1, "pools": {
        "p": _raw_pool_n_candidates(3, max_attempts=2)}}
    parsed, issues = load_pools_dict(raw, current_revision=1, strict=True)
    assert parsed is not None and issues == []
    assert parsed.pools["p"].max_attempts == 3


def test_legacy_load_auto_extends_identically():
    # Review Focus #3：磁盘存量载入与 CAS 写入同语义（自动扩展）
    raw = {"schema_version": 1, "pools": {
        "p": _raw_pool_n_candidates(3, max_attempts=2)}}
    parsed, issues = load_pools_dict(raw, current_revision=1, strict=False)
    assert parsed is not None and issues == []
    assert parsed.pools["p"].max_attempts == 3


def test_auto_extend_logs_warning_with_effective_value(caplog):
    # 告警必须带有效值，操作者可在日志/面板看到自愈结果
    import logging
    raw = {"schema_version": 1, "pools": {
        "p": _raw_pool_n_candidates(3, max_attempts=2)}}
    with caplog.at_level(logging.WARNING,
                         logger="inferfabric.model_pool.config"):
        load_pools_dict(raw, current_revision=1, strict=True)
    assert any("auto-extended" in r.message and "3" in r.message
               for r in caplog.records)


def test_attempts_equal_candidate_count_untouched():
    raw = {"schema_version": 1, "pools": {
        "p": _raw_pool_n_candidates(3, max_attempts=3)}}
    parsed, issues = load_pools_dict(raw, current_revision=1, strict=True)
    assert parsed is not None and issues == []
    assert parsed.pools["p"].max_attempts == 3


class TestStrategyParse:
    """WS2（2026-11-24）：组级回退策略 — 枚举严格校验，未知值/类型错即拒载。"""

    def test_strategy_default_is_priority_failover(self):
        raw = _raw_file(pools={"fast": _raw_pool()})
        raw["pools"]["fast"].pop("strategy", None)
        cfg, issues = load_pools_dict(raw, current_revision=0)
        assert issues == []
        assert cfg.pools["fast"].strategy == "priority_failover"

    def test_first_only_accepted(self):
        cfg, issues = load_pools_dict(
            _raw_file(pools={"fast": _raw_pool(strategy="first_only")}),
            current_revision=0)
        assert issues == []
        assert cfg.pools["fast"].strategy == "first_only"

    def test_unknown_strategy_rejected(self):
        cfg, issues = load_pools_dict(
            _raw_file(pools={"fast": _raw_pool(strategy="random_walk")}),
            current_revision=0)
        assert cfg is None
        assert any("strategy" in i for i in issues)

    def test_non_string_strategy_rejected(self):
        cfg, issues = load_pools_dict(
            _raw_file(pools={"fast": _raw_pool(strategy=True)}),
            current_revision=0)
        assert cfg is None
        assert any("strategy" in i for i in issues)

    def test_legacy_priority_alias_normalized(self):
        # #45 评审 R1：7.12.1 前控制台建池骨架写入的旧值 — 归一化到默认
        # 行为（旧 runtime 本就忽略 strategy），拒载会让存量 YAML 升级后
        # 整份失效（重启后全部 fabric-* 404）
        cfg, issues = load_pools_dict(
            _raw_file(pools={"fast": _raw_pool(strategy="priority")}),
            current_revision=0)
        assert issues == []
        assert cfg.pools["fast"].strategy == "priority_failover"
