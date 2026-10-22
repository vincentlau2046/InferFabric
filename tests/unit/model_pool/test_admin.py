"""model_pool.admin — 管理接口（GET/PUT/validate/status/dry-run，零出网）。"""

import time
from types import SimpleNamespace

import pytest

import inferfabric.model_pool.admin as mp_admin
from inferfabric.model_pool.registry import PoolRegistry

from tests.unit.model_pool.test_dispatch_openai import (
    FakeAuth,
    FakePM,
    _local_model,
    _raw_config,
)


class AdminHandler:
    """模拟 ProxyHandler：_read_body 返回预置 body，_send_json 捕获。"""

    def __init__(self, body=None):
        self.headers = {}
        self.path = "/admin/model-pools"
        self._body = body
        self.sent = []
        self._req_start = time.monotonic()

    def _read_body(self):
        return self._body

    def _send_json(self, data, status=200, extra_headers=None):
        self.sent.append((status, data))


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


@pytest.fixture
def pm(registry):
    return FakePM(registry, active={"qwen-small"},
                  local_models={"qwen-small": _local_model()},
                  cloud_models={"prov/cloud-m": object()},
                  providers={"prov": object()})


class TestGetModelPools:
    def test_get_returns_revision_and_pool_details(self, pm):
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, pm)

        status, body = h.sent[-1]
        assert status == 200
        assert body["revision"] == 1
        pool = body["pools"]["fast"]
        assert pool["enabled"] is True
        assert pool["allowed_locations"] == ["local", "cloud"]
        assert len(pool["candidates"]) == 2
        cand = pool["candidates"][0]
        assert cand["id"] == "loc"
        assert cand["kind"] == "local"
        assert cand["target"] == "qwen-small"
        assert cand["evidence"]["openai"] == "declared"  # 证据态区分 declared/probed

    def test_get_empty_registry(self, tmp_path):
        reg = PoolRegistry(config_path=tmp_path / "absent.yaml",
                           evidence_path=tmp_path / "e.json")
        reg.load()
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, FakePM(reg))
        status, body = h.sent[-1]
        assert status == 200
        assert body == {"revision": 0, "pools": {}}


class TestPutModelPools:
    def test_put_success_increments_revision(self, pm):
        cfg = _raw_config()
        cfg["expected_revision"] = 1
        h = AdminHandler(body=cfg)
        mp_admin.handle_put_model_pools(h, pm)

        status, body = h.sent[-1]
        assert status == 200
        assert body == {"status": "updated", "revision": 2}
        assert pm.pool_registry.snapshot().revision == 2

    def test_put_stale_revision_409(self, pm):
        cfg = _raw_config()
        cfg["expected_revision"] = 0  # 过期（当前 1）
        h = AdminHandler(body=cfg)
        mp_admin.handle_put_model_pools(h, pm)

        status, body = h.sent[-1]
        assert status == 409
        assert body["status"] == "revision_conflict"
        assert body["current_revision"] == 1
        assert pm.pool_registry.snapshot().revision == 1  # 未丢更新

    def test_put_invalid_config_400(self, pm):
        cfg = _raw_config()
        cfg["schema_version"] = 99
        cfg["expected_revision"] = 1
        h = AdminHandler(body=cfg)
        mp_admin.handle_put_model_pools(h, pm)

        status, body = h.sent[-1]
        assert status == 400
        assert body["status"] == "invalid_config"
        assert body["issues"]
        assert pm.pool_registry.snapshot().revision == 1  # 旧快照保留

    def test_put_missing_expected_revision_400(self, pm):
        h = AdminHandler(body=_raw_config())
        mp_admin.handle_put_model_pools(h, pm)
        status, body = h.sent[-1]
        assert status == 400


class TestValidate:
    def test_validate_valid_config(self, pm):
        h = AdminHandler(body=_raw_config())
        mp_admin.handle_validate_model_pools(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert body["valid"] is True
        assert body["issues"] == []

    def test_validate_invalid_config(self, pm):
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"] = [
            {"id": "a", "priority": 1, "kind": "cloud", "provider": "p",
             "model_id": "m"},
            {"id": "a", "priority": 1, "kind": "cloud", "provider": "p",
             "model_id": "m2"},
        ]
        h = AdminHandler(body=cfg)
        mp_admin.handle_validate_model_pools(h, pm)
        status, body = h.sent[-1]
        assert body["valid"] is False
        assert body["issues"]

    def test_validate_does_not_change_state(self, pm):
        h = AdminHandler(body=_raw_config())
        mp_admin.handle_validate_model_pools(h, pm)
        assert pm.pool_registry.snapshot().revision == 1
        # 零出网：FakePM.make_conn / forwarder 均会 AssertionError，未触发即通过


class TestStatus:
    def test_status_covers_both_protocols(self, pm):
        h = AdminHandler()
        mp_admin.handle_get_model_pools_status(h, pm)

        status, body = h.sent[-1]
        assert status == 200
        pool = body["pools"]["fast"]
        assert set(pool["protocols"]) == {"openai", "anthropic"}
        # 候选只声明 openai → openai 有 chosen，anthropic 报 no_evidence
        assert pool["protocols"]["openai"]["chosen"] == "loc"
        assert pool["protocols"]["anthropic"]["chosen"] is None
        reasons = {d["id"]: d["reason"]
                   for d in pool["protocols"]["anthropic"]["decisions"]}
        assert set(reasons.values()) == {"no_evidence"}

    def test_status_dangling_targets_listed(self, tmp_path):
        reg = PoolRegistry(
            config_path=tmp_path / "m.yaml", evidence_path=tmp_path / "e.json",
            known_local=set(),  # qwen-small 悬空
            known_providers={"prov"})
        reg.update(_raw_config(), expected_revision=0)
        pm2 = FakePM(reg)
        h = AdminHandler()
        mp_admin.handle_get_model_pools_status(h, pm2)
        _, body = h.sent[-1]
        assert any("qwen-small" in i for i in body["pools"]["fast"]["issues"])


class TestDryRun:
    def test_dry_run_openai_selects_local(self, pm):
        h = AdminHandler(body={"pool": "fast", "protocol": "openai"})
        mp_admin.handle_post_model_pools_dry_run(h, pm)

        status, body = h.sent[-1]
        assert status == 200
        assert body["chosen"] == "loc"
        assert body["error"] is None
        assert body["decisions"][0] == {"id": "loc", "included": True, "reason": "ok"}

    def test_dry_run_anthropic_explains_exclusion(self, pm):
        h = AdminHandler(body={"pool": "fast", "protocol": "anthropic"})
        mp_admin.handle_post_model_pools_dry_run(h, pm)

        status, body = h.sent[-1]
        assert status == 200
        assert body["chosen"] is None
        assert body["error"] == "no_compatible_target"

    def test_dry_run_with_requirements(self, pm):
        h = AdminHandler(body={"pool": "fast", "protocol": "openai",
                               "tools": True, "stream": True, "vision": True})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        _, body = h.sent[-1]
        # 无候选声明 vision → 全排除
        assert body["chosen"] is None
        reasons = {d["reason"] for d in body["decisions"]}
        assert "feature_unsupported" in reasons

    def test_dry_run_unknown_pool_404(self, pm):
        h = AdminHandler(body={"pool": "nope", "protocol": "openai"})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        status, body = h.sent[-1]
        assert status == 404
        assert body["status"] == "unknown_pool"

    def test_dry_run_zero_side_effects(self, pm):
        before = pm.pool_registry.snapshot()
        h = AdminHandler(body={"pool": "fast", "protocol": "openai"})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        assert pm.pool_registry.snapshot() is before
        assert pm.dual_gate.acquired == []   # 不占限流
        assert pm.ensure_service_calls == []  # 不启动模型
