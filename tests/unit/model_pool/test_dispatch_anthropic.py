"""model_pool.dispatch — Anthropic /v1/messages 入口 iff/ 池调度（fake pm）。"""

import time
from types import SimpleNamespace

import pytest

import inferfabric.model_pool.dispatch as dispatch
from inferfabric.model_pool.registry import PoolRegistry

from tests.unit.model_pool.test_dispatch_openai import (
    FakeAnomalies,
    FakeAuth,
    FakeCloud,
    FakeGate,
    FakeHandler,
    FakeLogger,
    FakeMgr,
    FakePM,
    _local_model,
    _raw_config,
)


class FakeMessagesHandler(FakeHandler):
    def __init__(self, auth_header="key-ok"):
        super().__init__(auth_header)
        self.path = "/v1/messages"


def _raw_config_anthropic():
    """候选声明 anthropic 协议（与 OpenAI 测试夹具同构、协议不同）。"""
    cfg = _raw_config()
    for c in cfg["pools"]["fast"]["candidates"]:
        c["declared_protocols"] = ["anthropic"]
    return cfg


@pytest.fixture
def registry(tmp_path):
    reg = PoolRegistry(
        config_path=tmp_path / "model_pools.yaml",
        evidence_path=tmp_path / "evidence.json",
        known_local={"qwen-small"},
        known_providers={"prov"},
        provider_base=lambda p: "https://base/v1",
    )
    reg.update(_raw_config_anthropic(), expected_revision=0)
    return reg


@pytest.fixture
def anth_local(monkeypatch):
    calls = []

    def fake_local(handler, pm, data, auth_header, model_obj, original_model,
                   suppress_client_errors=False):
        calls.append({"model": data.get("model"), "model_obj": model_obj,
                      "original": original_model, "auth": auth_header})
        return 200

    monkeypatch.setattr(dispatch.forwarder, "forward_anthropic_local", fake_local)
    return calls


class TestPoolMessagesDispatch:
    def test_local_active_candidate_forwarded(self, registry, anth_local):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert len(anth_local) == 1
        assert anth_local[0]["original"] == "iff/fast"
        assert anth_local[0]["model_obj"].served_name == "qwen-small"
        assert pm.ensure_service_calls == []          # ready_only
        assert pm.dual_gate.acquired[:1] == ["iff/fast"]  # 外层逻辑名 gate
        assert pm.dual_gate.acquired[1:] == ["qwen-small"]  # 物理目标 gate（F16）  # 限流键 = 逻辑名
        assert h.sent == []
        assert any(getattr(e, "route", None) == "pool:fast/local:qwen-small"
                   and getattr(e, "model", None) == "iff/fast"
                   for e in pm.logger.entries)

    def test_inactive_local_falls_to_cloud_anthropic_protocol(self, registry, anth_local, monkeypatch):
        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False):
            cloud_calls.append({"model": data["model"], "protocol": protocol})
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active=set(),
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert anth_local == []
        assert cloud_calls == [{"model": "cloud-m", "protocol": "anthropic"}]

    def test_anthropic_error_envelope_shape(self, registry):
        pm = FakePM(registry, active=set(), local_models={}, cloud_models={})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        status, body, hdrs = h.sent[-1]
        # Anthropic 错误信封：type=error + error{type,message}
        assert status == 503
        assert body["type"] == "error"
        assert body["error"]["type"] == "overloaded_error"
        assert isinstance(body["error"]["message"], str)
        assert any(k.lower() == "retry-after" for k in (hdrs or {}))

    def test_unknown_pool_404_anthropic_envelope(self, registry):
        pm = FakePM(registry)
        h = FakeMessagesHandler()
        data = {"model": "iff/nope", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/nope") is True
        status, body, _ = h.sent[-1]
        assert status == 404
        assert body == {"type": "error",
                        "error": {"type": "not_found_error", "message": body["error"]["message"]}}

    def test_auth_rejected_401_anthropic_envelope(self, registry):
        pm = FakePM(registry, auth=FakeAuth(models=["other"], name="guest"))
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 401
        assert body["error"]["type"] == "authentication_error"

    def test_protocol_mismatch_400_invalid_request(self, registry):
        # 池只有 openai 声明候选 → anthropic 入站无兼容
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"] = [
            {"id": "cld", "priority": 1, "kind": "cloud",
             "provider": "prov", "model_id": "cloud-m",
             "declared_protocols": ["openai"]}]
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["error"]["type"] == "invalid_request_error"

    def test_non_pool_prefix_returns_false(self, registry):
        pm = FakePM(registry)
        h = FakeMessagesHandler()
        data = {"model": "qwen-small", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "qwen-small") is False

    def test_switching_state_does_not_block_pool(self, registry, anth_local):
        # 池调度不切换本地模型，因此 profile_state=SWITCHING 不适用（不读该状态）
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        pm.mgr.state = SimpleNamespace(
            get=lambda key, default="": "SWITCHING" if key == "profile_state" else default)
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert len(anth_local) == 1

    def test_anthropic_vision_blocks_text_only(self, registry):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "x"}},
            ]}]}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["error"]["type"] == "invalid_request_error"

    def test_tools_requirement_checked(self, registry, anth_local):
        cfg = _raw_config_anthropic()
        cfg["pools"]["fast"]["candidates"][0]["features"] = ["tools"]
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": [],
                "tools": [{"name": "t", "input_schema": {}}]}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert len(anth_local) == 1
