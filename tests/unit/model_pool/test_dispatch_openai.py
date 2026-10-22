"""model_pool.dispatch — OpenAI 入口 iff/ 池调度（fake pm，无网络/GPU）。

核心不变量：
- 本地候选 ready_only：绝不调用 pm.ensure_service
- 池请求跳过响应缓存（GET/PUT 均不触碰）
- 鉴权用完整逻辑名（iff/fast），不做 split("/")[-1]
- 错误契约：unknown_pool 404 / no_compatible_target 400 / no_ready_target 503
"""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import inferfabric.model_pool.dispatch as dispatch
from inferfabric.model_pool.registry import PoolRegistry


# ── fakes ──

class FakeAuth:
    def __init__(self, enabled=True, models=None, name="primary"):
        self.enabled = enabled
        self._models = models  # None = 全部
        self._name = name

    def check(self, bearer_token, model):
        if bearer_token != "key-ok":
            return False, "invalid key"
        if self._models is not None and model not in self._models:
            return False, f"model '{model}' not allowed"
        return True, "ok"

    def key_name(self, bearer_token):
        return self._name


class FakeGate:
    def __init__(self):
        self.acquired = []
        self.released = 0

    def acquire(self, model, timeout=None):
        self.acquired.append(model)
        return SimpleNamespace(ok=True, reason="", release=self.release)

    def release(self):
        self.released += 1


class FakeCloud:
    def __init__(self, models=None, providers=None):
        self.cloud_models = models or {}
        self._providers = providers or {}

    def get_provider_config(self, name):
        return self._providers.get(name)


class FakeMgr:
    def __init__(self, active=(), models=None):
        self.active_services = set(active)
        self._models = models or {}

    def find_model_by_served_name(self, name):
        return self._models.get(name)

    def get_model(self, name):
        for m in self._models.values():
            if m.name == name:
                return m
        return None


class FakeLogger:
    def __init__(self):
        self.entries = []

    def log(self, entry):
        self.entries.append(entry)


class FakeAnomalies:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


class FakePM:
    def __init__(self, registry, auth=None, active=(), local_models=None,
                 cloud_models=None, providers=None):
        self.pool_registry = registry
        self.auth = auth or FakeAuth()
        self.mgr = FakeMgr(active=active, models=local_models or {})
        self.cloud = FakeCloud(models=cloud_models or {}, providers=providers or {})
        self.dual_gate = FakeGate()
        self.logger = FakeLogger()
        self.anomalies = FakeAnomalies()
        self.response_cache = None  # 显式 None；池代码不得调用其方法
        self.ensure_service_calls = []

    def new_request_id(self):
        return "req-1"

    def ensure_service(self, target):
        self.ensure_service_calls.append(target)
        raise AssertionError("pool dispatch must never call ensure_service")

    def get_target_port(self, model):
        m = self.mgr.find_model_by_served_name(model)
        return m.port if m else None

    def release_port(self, model, port):
        pass

    def make_conn(self, port):
        raise AssertionError("tests must monkeypatch _forward_request")


class FakeHandler:
    def __init__(self, auth_header="key-ok"):
        self.headers = {"Authorization": auth_header}
        self.path = "/v1/chat/completions"
        self.sent = []
        self._req_start = time.monotonic()
        self._agent_hit = SimpleNamespace(agent="t", ua="ua", x_app="x")

    def _send_json(self, data, status=200, extra_headers=None):
        self.sent.append((status, data, extra_headers))


# ── fixtures ──

def _local_model(name="qwen-small", served="qwen-small", port=8101, type_="vllm"):
    return SimpleNamespace(name=name, served_name=served, port=port, type=type_,
                           is_ollama=False, is_ollama_cpp=False, ollama=None,
                           ollama_cpp=None, replicas=None)


def _raw_config():
    return {
        "schema_version": 1,
        "pools": {
            "fast": {
                "enabled": True,
                "allowed_locations": ["local", "cloud"],
                "accept_declared": True,  # 显式接受管理员声明（spec §5.1）
                "candidates": [
                    {"id": "loc", "priority": 1, "kind": "local",
                     "model_name": "qwen-small", "declared_protocols": ["openai"]},
                    {"id": "cld", "priority": 2, "kind": "cloud",
                     "provider": "prov", "model_id": "cloud-m",
                     "declared_protocols": ["openai"]},
                ],
            },
        },
    }


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
def forwarded(monkeypatch):
    calls = []

    def fake_forward(handler, pm, target_port, body, stream, model_name="",
                     upstream_path=None, cache_body=None):
        calls.append({"port": target_port, "body": json.loads(body),
                      "stream": stream, "cache_body": cache_body,
                      "model_name": model_name})
        return True

    monkeypatch.setattr(dispatch, "_forward_request", fake_forward)
    return calls


# ── tests ──

class TestPoolChatDispatch:
    def test_local_active_candidate_forwarded_with_rewrite(self, registry, forwarded):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True

        assert len(forwarded) == 1
        assert forwarded[0]["body"]["model"] == "qwen-small"  # 改写为 served_name
        assert forwarded[0]["cache_body"] is None             # 池请求不写缓存
        assert pm.ensure_service_calls == []                  # 绝不自动启动
        assert pm.dual_gate.acquired[:1] == ["iff/fast"]  # 外层逻辑名 gate
        assert pm.dual_gate.acquired[1:] == ["qwen-small"]  # 物理目标 gate（F16）          # 限流键 = 逻辑名
        assert h.sent == []                                   # 无错误响应
        # RequestLog 记录逻辑名 + pool 路由
        assert any(getattr(e, "route", None) == "pool:fast/local:qwen-small"
                   and getattr(e, "model", None) == "iff/fast"
                   for e in pm.logger.entries)

    def test_inactive_local_falls_through_to_cloud(self, registry, forwarded, monkeypatch):
        cloud_results = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False):
            cloud_results.append({"model": data["model"], "protocol": protocol,
                                  "original": original_model})
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active=set(),  # qwen-small 未激活
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert forwarded == []                    # 本地零转发
        assert len(cloud_results) == 1
        assert cloud_results[0]["model"] == "cloud-m"
        assert cloud_results[0]["original"] == "iff/fast"

    def test_no_compatible_target_400_openai_envelope(self, registry):
        pm = FakePM(registry, active=set(), local_models={},
                    cloud_models={})  # 全部未就绪
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 503  # 候选存在但未就绪 → no_ready_target
        assert body["status"] == "no_ready_target"
        assert isinstance(body["error"], str)

    def test_protocol_mismatch_400(self, registry):
        # 候选只有 openai 声明；构造 anthropic-only 池来测 no_compatible
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"] = [
            {"id": "cld", "priority": 1, "kind": "cloud",
             "provider": "prov", "model_id": "cloud-m",
             "declared_protocols": ["anthropic"]}]
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["status"] == "no_compatible_target"

    def test_guest_without_pool_whitelist_rejected(self, registry, forwarded):
        # guest 白名单只含其它模型 → 完整名 iff/fast 不在白名单 → 401
        pm = FakePM(registry, auth=FakeAuth(models=["other-model"], name="guest"),
                    active={"qwen-small"}, local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 401
        assert forwarded == []

    def test_guest_with_pool_whitelist_allowed(self, registry, forwarded):
        pm = FakePM(registry, auth=FakeAuth(models=["iff/fast"], name="guest"),
                    active={"qwen-small"}, local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1

    def test_non_pool_prefix_returns_false(self, registry, forwarded):
        pm = FakePM(registry)
        h = FakeHandler()
        data = {"model": "qwen-small", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "qwen-small") is False
        assert forwarded == [] and h.sent == []

    def test_unknown_pool_404(self, registry, forwarded):
        pm = FakePM(registry)
        h = FakeHandler()
        data = {"model": "iff/nope", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/nope") is True
        status, body, _ = h.sent[-1]
        assert status == 404
        assert body["status"] == "unknown_pool"

    def test_empty_registry_404_not_crash(self, tmp_path):
        reg = PoolRegistry(config_path=tmp_path / "absent.yaml",
                           evidence_path=tmp_path / "e.json")
        reg.load()
        pm = FakePM(reg)
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert h.sent[-1][0] == 404

    def test_pool_request_never_touches_response_cache(self, registry, forwarded):
        class BoomCache:
            def get(self, *a, **k):
                raise AssertionError("pool request must not read cache")

            def put(self, *a, **k):
                raise AssertionError("pool request must not write cache")

        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        pm.response_cache = BoomCache()
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [], "temperature": 0}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1

    def test_invalid_key_401(self, registry):
        pm = FakePM(registry)
        h = FakeHandler(auth_header="bad-key")
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert h.sent[-1][0] == 401

    def test_disabled_pool_503(self, registry):
        cfg = _raw_config()
        cfg["pools"]["fast"]["enabled"] = False
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, hdrs = h.sent[-1]
        assert status == 503
        assert body["status"] == "pool_disabled"
        assert any(k.lower() == "retry-after" for k in (hdrs or {}))

    def test_vision_request_to_text_only_candidates_rejected(self, registry):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": object()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 400
        assert body["status"] == "no_compatible_target"

    def test_tools_request_routed_but_normalized(self, registry, forwarded):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        # 候选需声明 tools 才可选
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"][0]["features"] = ["tools"]
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [], "tools": [
            {"name": "t", "description": "d", "input_schema": {"type": "object"}}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1
        sent_tools = forwarded[0]["body"]["tools"]
        assert sent_tools[0]["type"] == "function"  # Anthropic 风格已归一化

    def test_rate_limited_429(self, registry, forwarded):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        pm.dual_gate = SimpleNamespace(
            acquire=lambda m, timeout=None: SimpleNamespace(ok=False, reason="rpm"),
            release=lambda: None)
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": []}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 429
        assert forwarded == []
