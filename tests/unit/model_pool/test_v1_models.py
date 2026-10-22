"""model_pool — /v1/models 列出启用逻辑名（Task 10）。

契约：
- 启用且有可解析候选的池以 {"id": "iff/<name>", "object": "model",
  "owned_by": "iff-pool"} 出现 — 不合并候选能力（无 capability union）
- 禁用池 / 无可解析候选（全悬空）的池不出现
- registry 空时输出与现状完全一致（无 iff/ 条目）
- 直连模型 / 云模型条目不变
"""

from types import SimpleNamespace

import pytest

import inferfabric.proxy.handler as handler_mod
from inferfabric.model_pool.registry import PoolRegistry

from tests.unit.model_pool.test_dispatch_openai import (
    FakeHandler,
    FakePM,
    _local_model,
    _raw_config,
)


def _cloud_model_entry(model_id="cloud-m"):
    d = {"id": model_id, "object": "model", "owned_by": "cloud",
         "openai_available": True}
    return SimpleNamespace(to_api_dict=lambda: dict(d))


class V1PM(FakePM):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cloud_discovered = False

    def ensure_cloud_discovered(self):
        self.cloud_discovered = True


def _pm(registry):
    return V1PM(registry, active=(),
                local_models={"qwen-small": _local_model()},
                cloud_models={"cloud-m": _cloud_model_entry()},
                providers={"prov": SimpleNamespace()})


def _models_data(h):
    status, data, _ = h.sent[-1]
    assert status == 200
    return data["data"]


@pytest.fixture
def registry(tmp_path):
    reg = PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "evidence.json",
        known_local={"qwen-small"},
        known_providers={"prov"},
        provider_base=lambda p: "https://base/v1",
    )
    reg.update(_raw_config(), expected_revision=0)
    return reg


class TestV1ModelsListsPools:
    def test_enabled_pool_listed_as_iff_pool(self, registry):
        h = FakeHandler()
        handler_mod.ProxyHandler._handle_v1_models(h, _pm(registry))
        entries = {m["id"]: m for m in _models_data(h)}
        assert "iff/fast" in entries
        # 不合并候选能力：条目只含 id/object/owned_by
        assert entries["iff/fast"] == {"id": "iff/fast", "object": "model",
                                       "owned_by": "iff-pool"}

    def test_disabled_pool_not_listed(self, registry):
        cfg = _raw_config()
        cfg["pools"]["fast"]["enabled"] = False
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        h = FakeHandler()
        handler_mod.ProxyHandler._handle_v1_models(h, _pm(registry))
        assert all(m["id"] != "iff/fast" for m in _models_data(h))

    def test_pool_without_resolvable_candidates_not_listed(self, tmp_path):
        # 全部候选悬空（models.d / provider 均不存在）→ 有 issues 无 candidates → 不列出
        reg = PoolRegistry(config_path=tmp_path / "m.yaml",
                           evidence_path=tmp_path / "e.json",
                           known_local=set(), known_providers=set())
        reg.update(_raw_config(), expected_revision=0)
        h = FakeHandler()
        handler_mod.ProxyHandler._handle_v1_models(h, _pm(reg))
        assert all(m["id"] != "iff/fast" for m in _models_data(h))

    def test_direct_and_cloud_entries_unchanged(self, registry):
        h = FakeHandler()
        handler_mod.ProxyHandler._handle_v1_models(h, _pm(registry))
        entries = {m["id"]: m for m in _models_data(h)}
        assert entries["qwen-small"] == {"id": "qwen-small", "object": "model",
                                         "owned_by": "local", "type": "vllm"}
        assert entries["cloud-m"]["owned_by"] == "cloud"

    def test_empty_registry_output_identical_to_current(self, tmp_path):
        reg = PoolRegistry(config_path=tmp_path / "absent.yaml",
                           evidence_path=tmp_path / "e.json")
        reg.load()
        h = FakeHandler()
        handler_mod.ProxyHandler._handle_v1_models(h, _pm(reg))
        data = _models_data(h)
        # 无任何 iff/ 条目；本地 + 云条目与现状一致
        assert [m["id"] for m in data] == ["qwen-small", "cloud-m"]
        assert all(not m["id"].startswith("iff/") for m in data)
