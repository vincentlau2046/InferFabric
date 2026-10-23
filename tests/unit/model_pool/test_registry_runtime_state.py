"""Registry 运行状态（Phase 1 Task 3）：configured / last_load_error / raw_config / compile_pool。

契约（spec §4.3/§4.4）：
- configured=True：曾成功加载/写入过有效配置（含零池）
- configured=False + last_load_error=None：从未配置（无文件）→ 前端出初始化向导
- configured=False + last_load_error≠null：文件损坏/不可读 → 前端错误横幅
- raw_config()：最后成功加载/写入的原始配置深拷贝（编辑器读-改-写基底，防丢数据）
- compile_pool(config, pool_name)：dry-run 编辑态预览 — 只读不发布
- _load_locked 三态返回语义零变化（config_reloader 依赖）
"""

from pathlib import Path

import pytest

from inferfabric.model_pool.registry import PoolRegistry


def _make_registry(tmp_path: Path, known_local=("m-local",),
                   known_providers=("prov",)) -> PoolRegistry:
    return PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "pool_evidence.json",
        known_local=known_local,
        known_providers=known_providers,
        provider_route=lambda p: {"openai_base": "https://b/v1",
                                  "anthropic_base": "https://b/v1"},
    )


_VALID = {
    "schema_version": 1,
    "revision": 1,
    "pools": {
        "fast": {
            "enabled": True,
            "allowed_locations": ["local"],
            "accept_declared": True,
            "max_attempts": 2,
            "deadline_ms": 60000,
            "candidates": [{
                "id": "c1", "priority": 1, "kind": "local",
                "model_name": "m-local",
                "declared_protocols": ["openai", "anthropic"],
                "features": ["tools"],
            }],
        },
    },
}


class TestConfiguredState:
    def test_initial_state_unconfigured(self, tmp_path):
        reg = _make_registry(tmp_path)
        assert reg.configured is False
        assert reg.last_load_error is None
        assert reg.raw_config() is None

    def test_load_valid_file_sets_configured(self, tmp_path):
        import yaml
        reg = _make_registry(tmp_path)
        (tmp_path / "model_pools.yaml").write_text(
            yaml.safe_dump(_VALID), encoding="utf-8")
        reg.load()
        assert reg.configured is True
        assert reg.last_load_error is None
        raw = reg.raw_config()
        assert raw["pools"]["fast"]["candidates"][0]["id"] == "c1"
        # 深拷贝：改返回值不影响 registry 内部
        raw["pools"]["fast"]["enabled"] = False
        assert reg.raw_config()["pools"]["fast"]["enabled"] is True

    def test_missing_file_stays_unconfigured(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.load()
        assert reg.configured is False
        assert reg.last_load_error is None

    def test_invalid_file_sets_load_error_then_recovers(self, tmp_path):
        import yaml
        reg = _make_registry(tmp_path)
        cfg = tmp_path / "model_pools.yaml"
        cfg.write_text("pools: [not, a, mapping]", encoding="utf-8")
        reg.load()
        assert reg.configured is False
        assert reg.last_load_error  # 非空错误信息
        # 写入好文件 → reload → 错误清空
        cfg.write_text(yaml.safe_dump(_VALID), encoding="utf-8")
        result = reg.reload_from_disk()
        assert result is True          # 拾取新配置（三态语义不变）
        assert reg.configured is True
        assert reg.last_load_error is None

    def test_unreadable_file_sets_load_error(self, tmp_path):
        reg = _make_registry(tmp_path)
        cfg = tmp_path / "model_pools.yaml"
        cfg.write_text("\tbad: [unclosed", encoding="utf-8")  # YAML 语法错误
        reg.load()
        assert reg.configured is False
        assert reg.last_load_error

    def test_update_sets_configured(self, tmp_path):
        reg = _make_registry(tmp_path)
        reg.update(_VALID, expected_revision=0)
        assert reg.configured is True
        assert reg.last_load_error is None
        assert reg.raw_config()["pools"]["fast"]["candidates"][0]["id"] == "c1"

    def test_empty_file_is_invalid_with_error(self, tmp_path):
        reg = _make_registry(tmp_path)
        (tmp_path / "model_pools.yaml").write_text("", encoding="utf-8")
        reg.load()
        assert reg.configured is False
        assert reg.last_load_error


class TestCompilePool:
    def test_compile_pool_returns_snapshot_for_config(self, tmp_path):
        reg = _make_registry(tmp_path)
        ps = reg.compile_pool(_VALID, "fast")
        assert ps.pool.name == "fast"
        assert ps.candidates[0].candidate.id == "c1"
        # 候选已解析：含端点指纹
        assert ps.candidates[0].endpoint_fingerprint

    def test_compile_pool_invalid_raises(self, tmp_path):
        reg = _make_registry(tmp_path)
        with pytest.raises(ValueError):
            reg.compile_pool({"schema_version": 1, "pools": {"fast": "not-a-map"}},
                             "fast")

    def test_compile_pool_unknown_pool_raises(self, tmp_path):
        reg = _make_registry(tmp_path)
        with pytest.raises(KeyError):
            reg.compile_pool(_VALID, "nope")

    def test_compile_pool_does_not_publish(self, tmp_path):
        reg = _make_registry(tmp_path)
        before = reg.snapshot()
        reg.compile_pool(_VALID, "fast")
        assert reg.snapshot() is before          # 快照引用未变
        assert reg.raw_config() is None          # raw 未被写入
        assert reg.configured is False           # 状态未被推进

    def test_compile_pool_zero_pools_file_raises_keyerror(self, tmp_path):
        reg = _make_registry(tmp_path)
        with pytest.raises(KeyError):
            reg.compile_pool({"schema_version": 1, "pools": {}}, "any")
