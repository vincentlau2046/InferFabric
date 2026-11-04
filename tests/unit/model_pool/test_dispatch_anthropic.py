"""model_pool.dispatch — Anthropic /v1/messages 入口 iff/ 池调度（fake pm）。"""

import time
from types import SimpleNamespace

import pytest

import inferfabric.model_pool.dispatch as dispatch
from inferfabric.forwarder import CloudResult
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
        known_local={"qwen-small", "qwen-vl"},   # qwen-vl：视觉候选测试用
        known_providers={"prov"},
        provider_base=lambda p: "https://base/v1",
    )
    reg.update(_raw_config_anthropic(), expected_revision=0)
    return reg


@pytest.fixture
def anth_local(monkeypatch):
    calls = []

    def fake_local(handler, pm, data, auth_header, model_obj, original_model,
                   suppress_client_errors=False, timeout=None):
        calls.append({"model": data.get("model"), "model_obj": model_obj,
                      "original": original_model, "auth": auth_header})
        # Phase E 契约：suppress 模式返回 CloudResult（终审 #10）
        return CloudResult(status=200, committed=True)

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
        assert pm.dual_gate.target_acquired == ["qwen-small"]  # 物理目标 gate（acquire_target，终审 #8）
        assert h.sent == []
        assert any(getattr(e, "route", None) == "pool:fast/local:qwen-small"
                   and getattr(e, "model", None) == "qwen-small"  # 物理模型（终审 #15）
                   for e in pm.logger.entries)

    def test_inactive_local_falls_to_cloud_anthropic_protocol(self, registry, anth_local, monkeypatch):
        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False, timeout=None):
            cloud_calls.append({"model": data["model"], "protocol": protocol})
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        # 跨位置回退显式开启（终审 #2：本地未就绪不落云是默认行为）
        cfg = _raw_config_anthropic()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active=set(),
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1",
                        anthropic_base="https://base/v1")})
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
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1",
                        anthropic_base="https://base/v1")})
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

    def test_anthropic_vision_no_vision_capable_routes_by_priority(
            self, registry, anth_local):
        # 7.10.0：池内候选模型本体均无视觉 → 带图请求按优先级正常
        # 调用本地 p1（是否支持由模型/provider 自身处理；遗留 features 忽略）
        cfg = _raw_config_anthropic()
        for c in cfg["pools"]["fast"]["candidates"]:
            c["features"] = ["vision"]   # 遗留声明 — 读时忽略
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    model_dicts=[{"name": "qwen-small", "model_type": "llm"}],
                    cloud_models={"prov/cloud-m": SimpleNamespace(
                        supports_vision=None)})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "x"}},
            ]}]}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert len(anth_local) == 1              # 正常转发本地 p1
        assert h.sent == []                      # 无错误信封

    def test_anthropic_vision_routes_to_vision_capable(self, registry, anth_local):
        # 带图请求落到 model_type=vl 的本地候选（anthropic 直通路径）
        cfg = _raw_config_anthropic()
        cfg["pools"]["fast"]["candidates"].append(
            {"id": "vlloc", "priority": 2, "kind": "local",
             "model_name": "qwen-vl", "declared_protocols": ["anthropic"]})
        cfg["pools"]["fast"]["candidates"][1]["priority"] = 3
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small", "qwen-vl"},
                    local_models={"qwen-small": _local_model(),
                                  "qwen-vl": _local_model(name="qwen-vl",
                                                          served="qwen-vl",
                                                          port=8102)},
                    model_dicts=[{"name": "qwen-small", "model_type": "llm"},
                                 {"name": "qwen-vl", "model_type": "vl"}])
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "x"}},
            ]}]}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert len(anth_local) == 1
        assert anth_local[0]["model_obj"].served_name == "qwen-vl"

    def test_tools_requirement_checked(self, registry, anth_local):
        # 7.8.0：tools 是 agent 基础能力 — 空声明（默认）即可路由
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": [],
                "tools": [{"name": "t", "input_schema": {}}]}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert len(anth_local) == 1


# ── 候选不兼容故障转移（NInfer parallel_tool_use_not_supported，2026-11-04）──

class TestParallelToolUseFailover:
    """本地引擎 400 code=parallel_tool_use_not_supported → 候选对本请求
    不兼容 → 切下一候选（云 glm-5 接受 disable_parallel_tool_use）。
    其他 400（真实畸形请求）保持「终止不回退」既有语义。"""

    def _pm_with_cloud(self, registry, anth_local_code):
        """本地候选 qwen-small 在役，返回 (pm, cloud_calls, fake_local)。"""
        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False, timeout=None):
            cloud_calls.append({"model": data["model"], "protocol": protocol})
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        def fake_local(handler, pm, data, auth_header, model_obj, original_model,
                       suppress_client_errors=False, timeout=None):
            return CloudResult(status=400, committed=False,
                               error="Local upstream returned HTTP 400",
                               upstream_code=anth_local_code)

        return cloud_calls, fake_local, fake_cloud

    def test_parallel_tool_use_400_falls_to_cloud(self, registry, monkeypatch):
        cloud_calls, fake_local, fake_cloud = self._pm_with_cloud(registry, "parallel_tool_use_not_supported")
        monkeypatch.setattr(dispatch.forwarder, "forward_anthropic_local", fake_local)
        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        cfg = _raw_config_anthropic()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1",
                        anthropic_base="https://base/v1")})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert cloud_calls == [{"model": "cloud-m", "protocol": "anthropic"}]
        assert h.sent == []                      # 成功路径无错误信封

    def test_other_400_terminates_no_fallback(self, registry, monkeypatch):
        # 真实畸形请求（无 code）→ 终止不回退，云不被调用，发 400 信封
        cloud_calls, fake_local, fake_cloud = self._pm_with_cloud(registry, None)
        monkeypatch.setattr(dispatch.forwarder, "forward_anthropic_local", fake_local)
        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        cfg = _raw_config_anthropic()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1",
                        anthropic_base="https://base/v1")})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        assert cloud_calls == []
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"

    def test_all_candidates_incompatible_503_envelope(self, registry, monkeypatch):
        # 端到端：池内唯一本地候选不兼容 → 客户端收到干净的 503
        # pool_unavailable 信封，绝不见到上游 400
        def fake_local(handler, pm, data, auth_header, model_obj, original_model,
                       suppress_client_errors=False, timeout=None):
            return CloudResult(status=400, committed=False,
                               error="Local upstream returned HTTP 400",
                               upstream_code="parallel_tool_use_not_supported")

        monkeypatch.setattr(dispatch.forwarder, "forward_anthropic_local", fake_local)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={}, providers={})
        h = FakeMessagesHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_messages(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 503
        assert body["type"] == "error"
        assert body["error"]["type"] == "overloaded_error"
        assert any(k.lower() == "retry-after" for k in (h.sent[-1][2] or {}))
