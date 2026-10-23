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
        # v7.1.0 契约：三态键随空快照返回（configured=False → 前端出向导）
        assert body == {"revision": 0, "pools": {}, "configured": False,
                        "load_error": None, "raw": None}


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


class TestToggleEndpoint:
    """POST /admin/model-pools/toggle — 服务端原子翻转单池 enabled。

    dashboard 开关走此端点而非客户端读-改-写：GET 视图有损（悬空候选被
    剔除、declared_protocols 不回传），经视图重建配置会丢数据。
    """

    def test_toggle_success_flips_and_increments(self, pm, tmp_path):
        h = AdminHandler(body={"pool": "fast", "enabled": False,
                               "expected_revision": 1})
        mp_admin.handle_post_model_pools_toggle(h, pm)

        status, body = h.sent[-1]
        assert status == 200
        assert body == {"status": "updated", "revision": 2,
                        "pool": "fast", "enabled": False}
        assert pm.pool_registry.snapshot().pools["fast"].pool.enabled is False
        # 落盘同步（含悬空候选不丢失的原始结构）
        yaml_text = (tmp_path / "model_pools.yaml").read_text(encoding="utf-8")
        assert "enabled: false" in yaml_text

    def test_toggle_back_on_preserves_other_fields(self, pm):
        h = AdminHandler(body={"pool": "fast", "enabled": True,
                               "expected_revision": 1})
        mp_admin.handle_post_model_pools_toggle(h, pm)
        assert h.sent[-1][0] == 200
        pool = pm.pool_registry.snapshot().pools["fast"].pool
        assert pool.enabled is True
        assert pool.max_attempts == 2                   # 其余配置原样
        assert [c.candidate.id for c in pm.pool_registry.snapshot()
                .pools["fast"].candidates] == ["loc", "cld"]

    def test_toggle_stale_revision_409(self, pm):
        h = AdminHandler(body={"pool": "fast", "enabled": False,
                               "expected_revision": 0})
        mp_admin.handle_post_model_pools_toggle(h, pm)
        status, body = h.sent[-1]
        assert status == 409
        assert body["status"] == "revision_conflict"
        assert pm.pool_registry.snapshot().pools["fast"].pool.enabled is True

    def test_toggle_unknown_pool_404(self, pm):
        h = AdminHandler(body={"pool": "nope", "enabled": False,
                               "expected_revision": 1})
        mp_admin.handle_post_model_pools_toggle(h, pm)
        status, body = h.sent[-1]
        assert status == 404
        assert body["status"] == "unknown_pool"

    def test_toggle_missing_fields_400(self, pm):
        h = AdminHandler(body={"pool": "fast"})
        mp_admin.handle_post_model_pools_toggle(h, pm)
        assert h.sent[-1][0] == 400
        assert pm.pool_registry.snapshot().revision == 1  # 未变更

    def test_toggle_after_external_edit_uses_latest_raw(self, registry, tmp_path):
        # 外部编辑（无 revision 键）→ reload 拾取 → flip 基于最新原始结构
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"].append(
            {"id": "extra", "priority": 3, "kind": "cloud",
             "provider": "prov", "model_id": "cloud-m2",
             "declared_protocols": ["openai"]})
        import yaml as _yaml
        (tmp_path / "model_pools.yaml").write_text(
            _yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        registry.reload_from_disk()
        pm2 = FakePM(registry, active={"qwen-small"},
                     local_models={"qwen-small": _local_model()},
                     cloud_models={"prov/cloud-m": object()},
                     providers={"prov": object()})
        h = AdminHandler(body={"pool": "fast", "enabled": False,
                               "expected_revision": registry.snapshot().revision})
        mp_admin.handle_post_model_pools_toggle(h, pm2)
        assert h.sent[-1][0] == 200
        ids = [c.candidate.id for c in registry.snapshot().pools["fast"].candidates]
        assert "extra" in ids                           # 外部新增候选不丢失


# ── Phase 1 Task 4：GET 三态+raw / dry-run 编辑态 / runtime 端点 ──

from inferfabric.model_pool.pool_forward import CooldownStore


class TestGetRuntimeState:
    def test_get_includes_configured_raw_load_error(self, pm):
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert body["configured"] is True
        assert body["load_error"] is None
        # raw 是编辑基底：含视图不回传的字段（declared_protocols）
        raw_cand = body["raw"]["pools"]["fast"]["candidates"][0]
        assert raw_cand["declared_protocols"] == ["openai"]

    def test_get_unconfigured_returns_null_raw(self, tmp_path):
        reg = PoolRegistry(config_path=tmp_path / "absent.yaml",
                           evidence_path=tmp_path / "e.json")
        reg.load()
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, FakePM(reg))
        status, body = h.sent[-1]
        assert status == 200
        assert body["configured"] is False
        assert body["load_error"] is None
        assert body["raw"] is None

    def test_get_raw_is_json_serializable_with_yaml_dates(self, tmp_path):
        # 终审 I4：YAML safe_load 把 ISO 日期解析成 datetime.date，raw 原样
        # 透传 → _send_json 内 json.dumps TypeError → GET 整个 500。
        # 管理端点边界必须 JSON 安全。
        import datetime
        import json as _json
        import yaml as _yaml
        cfg = _raw_config()
        cfg["updated_at"] = datetime.date(2026, 10, 23)  # 用户在 YAML 里写日期
        (tmp_path / "m.yaml").write_text(
            _yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        reg = PoolRegistry(config_path=tmp_path / "m.yaml",
                           evidence_path=tmp_path / "e.json",
                           known_local={"qwen-small"}, known_providers={"prov"},
                           provider_base=lambda p: "https://base/v1")
        reg.load()
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, FakePM(reg))
        status, body = h.sent[-1]
        assert status == 200
        assert body["raw"]["updated_at"] == "2026-10-23"   # date → ISO 字符串
        _json.dumps(body)   # 不得 TypeError

    def test_get_revision_matches_raw_under_racing_update(self, pm, monkeypatch):
        # 终审 M6：snapshot() 与 raw_config() 两次取锁之间穿插 update →
        # 响应携带旧 revision + 新 raw（编辑器以旧基点 POST 新内容 → 反复 409）。
        # handler 必须走单锁原子读取，使 revision 与 raw 永远同代。
        reg = pm.pool_registry
        orig_raw_config = reg.raw_config

        def racing_raw_config():
            # 模拟并发 update 在 snapshot() 之后、raw_config() 之前落地
            cfg = _raw_config()
            cfg["pools"]["fast"]["candidates"].append(
                {"id": "racer", "priority": 9, "kind": "cloud",
                 "provider": "prov", "model_id": "cloud-x"})
            reg.update(cfg, expected_revision=reg.snapshot().revision)
            return orig_raw_config()   # 返回已推进到新一代的 raw

        monkeypatch.setattr(reg, "raw_config", racing_raw_config)
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert body["revision"] == body["raw"]["revision"]   # 同代不变式

    def test_get_broken_file_reports_load_error(self, tmp_path):
        reg = PoolRegistry(config_path=tmp_path / "broken.yaml",
                           evidence_path=tmp_path / "e.json")
        (tmp_path / "broken.yaml").write_text("pools: [bad]", encoding="utf-8")
        reg.load()
        h = AdminHandler()
        mp_admin.handle_get_model_pools(h, FakePM(reg))
        status, body = h.sent[-1]
        assert status == 200
        assert body["configured"] is False
        assert body["load_error"]


class TestDryRunWithConfig:
    def test_dry_run_with_config_previews_edited_candidates(self, pm):
        # 同池名，编辑态候选 ≠ 快照候选 → decisions 必须来自 config
        edited = {
            "schema_version": 1,
            "pools": {"fast": {
                "enabled": True,
                "allowed_locations": ["local"],
                "accept_declared": True,
                "candidates": [
                    {"id": "newloc", "priority": 1, "kind": "local",
                     "model_name": "qwen-small",
                     "declared_protocols": ["openai", "anthropic"]},
                ],
            }},
        }
        h = AdminHandler(body={"pool": "fast", "protocol": "openai",
                               "config": edited})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert body["chosen"] == "newloc"
        ids = [d["id"] for d in body["decisions"]]
        assert ids == ["newloc"]      # 不含快照候选 loc/cld

    def test_dry_run_with_config_invalid_400(self, pm):
        h = AdminHandler(body={"pool": "fast", "protocol": "openai",
                               "config": {"schema_version": 1,
                                          "pools": {"fast": "not-a-map"}}})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        status, body = h.sent[-1]
        assert status == 400
        assert body["status"] == "invalid_config"

    def test_dry_run_with_config_unknown_pool_404(self, pm):
        h = AdminHandler(body={"pool": "nope", "protocol": "openai",
                               "config": _raw_config()})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        status, body = h.sent[-1]
        assert status == 404
        assert body["status"] == "unknown_pool"

    def test_dry_run_without_config_unchanged(self, pm):
        # 不传 config → 走已发布快照（既有契约）
        h = AdminHandler(body={"pool": "fast", "protocol": "openai"})
        mp_admin.handle_post_model_pools_dry_run(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert body["chosen"] == "loc"


class TestRuntimeEndpoint:
    def test_runtime_endpoint_shape(self, pm):
        pm.pool_cooldowns = CooldownStore()
        h = AdminHandler()
        mp_admin.handle_get_model_pools_runtime(h, pm)
        status, body = h.sent[-1]
        assert status == 200
        assert body["revision"] == 1
        cands = body["pools"]["fast"]["candidates"]
        by_id = {c["id"]: c for c in cands}
        assert set(by_id) == {"loc", "cld"}
        # 本地候选：active=True（在 active_services）；云候选：active=True（已发现）
        assert by_id["loc"]["active"] is True
        assert by_id["cld"]["active"] is True
        assert by_id["loc"]["cooling"] is None
        assert by_id["loc"]["last_attempt"] is None
        assert by_id["loc"]["fingerprint"]

        # 注入冷却 + 最近失败 → 三态各出现一次
        # （用真实 monotonic 注入 — handler 以真实时钟读取，混用假时钟
        #   会得到负 remaining_s，Review Focus #5 的混时钟陷阱）
        fp = by_id["loc"]["fingerprint"]
        t = time.monotonic()
        pm.pool_cooldowns.add(fp, now=t, reason="cooldown_401")
        pm.pool_cooldowns.record_attempt(fp, ok=False, status=503, now=t)
        h2 = AdminHandler()
        mp_admin.handle_get_model_pools_runtime(h2, pm)
        _, body2 = h2.sent[-1]
        c = {x["id"]: x for x in body2["pools"]["fast"]["candidates"]}["loc"]
        assert c["cooling"]["reason"] == "cooldown_401"
        assert c["cooling"]["probing"] is False
        assert c["cooling"]["remaining_s"] > 0
        assert c["last_attempt"]["status"] == 503
        assert c["last_attempt"]["ok"] is False

    def test_runtime_endpoint_inactive_states(self, pm):
        # 本地未启动 / 云未发现
        pm2 = FakePM(pm.pool_registry, active=set(),
                     local_models={"qwen-small": _local_model()},
                     cloud_models={}, providers={"prov": object()})
        h = AdminHandler()
        mp_admin.handle_get_model_pools_runtime(h, pm2)
        _, body = h.sent[-1]
        by_id = {c["id"]: c for c in body["pools"]["fast"]["candidates"]}
        assert by_id["loc"]["active"] is False
        assert by_id["cld"]["active"] is False

    def test_runtime_endpoint_empty_registry(self, tmp_path):
        reg = PoolRegistry(config_path=tmp_path / "absent.yaml",
                           evidence_path=tmp_path / "e.json")
        reg.load()
        h = AdminHandler()
        mp_admin.handle_get_model_pools_runtime(h, FakePM(reg))
        status, body = h.sent[-1]
        assert status == 200
        assert body == {"revision": 0, "pools": {}}

    def test_runtime_endpoint_registered_in_get_routes(self):
        from inferfabric.proxy.handler import _GET_ROUTES
        assert "/admin/model-pools/runtime" in _GET_ROUTES
