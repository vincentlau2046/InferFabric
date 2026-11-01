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
        self.target_acquired = []
        self.released = 0

    def acquire(self, model, timeout=None):
        self.acquired.append(model)
        return SimpleNamespace(ok=True, reason="", release=self.release)

    def acquire_target(self, model, timeout=None):
        self.target_acquired.append(model)
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
    def __init__(self, active=(), models=None, model_dicts=None):
        self.active_services = set(active)
        self._models = models or {}
        # list_models 数据源（dict 列表，同真实 manager）— runtime_view
        # 据此构建 local_vision（models.d model_type=vl）
        self._model_dicts = list(model_dicts or [])

    def find_model_by_served_name(self, name):
        return self._models.get(name)

    def get_model(self, name):
        for m in self._models.values():
            if m.name == name:
                return m
        return None

    def list_models(self):
        return self._model_dicts


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
                 cloud_models=None, providers=None, model_dicts=None):
        self.pool_registry = registry
        self.auth = auth or FakeAuth()
        self.mgr = FakeMgr(active=active, models=local_models or {},
                           model_dicts=model_dicts)
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
        known_local={"qwen-small", "qwen-vl"},   # qwen-vl：视觉候选测试用
        known_providers={"prov"},
        provider_base=lambda p: "https://base/v1",
    )
    reg.update(_raw_config(), expected_revision=0)
    return reg


@pytest.fixture
def forwarded(monkeypatch):
    calls = []

    def fake_forward(handler, pm, target_port, body, stream, model_name="",
                     upstream_path=None, cache_body=None, timeout=None):
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
        assert pm.dual_gate.acquired == ["iff/fast"]        # 外层逻辑名 gate（限流键）
        assert pm.dual_gate.target_acquired == ["qwen-small"]  # 物理目标门= acquire_target
        assert h.sent == []                                   # 无错误响应
        # RequestLog 记录逻辑名 + pool 路由
        assert any(getattr(e, "route", None) == "pool:fast/local:qwen-small"
                   and getattr(e, "model", None) == "qwen-small"  # 物理目标（终审 #15）
                   for e in pm.logger.entries)

    def test_inactive_local_falls_through_to_cloud(self, registry, forwarded, monkeypatch):
        cloud_results = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False, timeout=None):
            cloud_results.append({"model": data["model"], "protocol": protocol,
                                  "original": original_model})
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        # 跨位置回退显式开启（终审 #2：本地未就绪不落云是默认行为）
        cfg = _raw_config()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active=set(),  # qwen-small 未激活
                    cloud_models={"prov/cloud-m": object()},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1",
                        anthropic_base="https://base/v1")})
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
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1",
                        anthropic_base="https://base/v1")})
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

    def test_vision_request_no_vision_capable_routes_by_priority(
            self, registry, forwarded):
        # 7.10.0：池内候选模型本体均无视觉（本地 llm / 云未声明
        # supports_vision）→ 带图请求按优先级正常调用 p1（是否支持由
        # 模型/provider 自身处理）；遗留 features 声明不构成任何路由行为
        cfg = _raw_config()
        for c in cfg["pools"]["fast"]["candidates"]:
            c["features"] = ["vision"]   # 遗留声明 — 读时忽略
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    model_dicts=[{"name": "qwen-small", "model_type": "llm"}],
                    cloud_models={"prov/cloud-m": SimpleNamespace(
                        supports_vision=None)})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1
        assert forwarded[0]["port"] == 8101   # 纯优先级 → 本地 p1
        assert h.sent == []                   # 无错误信封

    def test_vision_metadata_failure_unknown_local_routes_by_priority(
            self, registry, forwarded, monkeypatch):
        # 7.10.0：mgr.list_models 异常 = 本地视觉能力全部未知（None 三态）
        # → 不构成偏好也不排除，云未声明（None）同样无偏好 → 纯优先级
        # 走本地 p1（元数据故障不得改变路由结果）
        cfg = _raw_config()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False, timeout=None):
            cloud_calls.append(data["model"])
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": SimpleNamespace(
                        supports_vision=None)},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1")})

        def boom():
            raise RuntimeError("models.d unreadable")
        monkeypatch.setattr(pm.mgr, "list_models", boom)

        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1
        assert forwarded[0]["port"] == 8101   # 纯优先级 → 本地 p1
        assert cloud_calls == []

    def test_vision_prefers_declared_cloud_vl_over_unknown_local(
            self, registry, forwarded, monkeypatch):
        # 7.10.0（用户裁决「VL 优先路由 VL」）：本地视觉元数据故障（未知）
        # 时，唯一已知 vision-capable 的云候选获得软优先 — 带图请求合法
        # 落云（这是偏好而非元数据故障改道：判定源是云模型本体声明）
        cfg = _raw_config()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        cloud_calls = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False, timeout=None):
            cloud_calls.append(data["model"])
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()},
                    cloud_models={"prov/cloud-m": SimpleNamespace(
                        supports_vision=True)},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1")})

        def boom():
            raise RuntimeError("models.d unreadable")
        monkeypatch.setattr(pm.mgr, "list_models", boom)

        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert cloud_calls == ["cloud-m"]      # 已知 VL 云候选软优先
        assert forwarded == []

    def test_vision_request_routes_to_vision_capable_candidate(self, registry, forwarded):
        # 带图请求越过 p1 文本候选，落到 p2 的 VL 本地模型（model_type=vl）
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"].append(
            {"id": "vlloc", "priority": 2, "kind": "local",
             "model_name": "qwen-vl", "declared_protocols": ["openai"]})
        cfg["pools"]["fast"]["candidates"][1]["priority"] = 3
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small", "qwen-vl"},
                    local_models={"qwen-small": _local_model(),
                                  "qwen-vl": _local_model(name="qwen-vl",
                                                          served="qwen-vl",
                                                          port=8102)},
                    model_dicts=[{"name": "qwen-small", "model_type": "llm"},
                                 {"name": "qwen-vl", "model_type": "vl"}])
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1
        assert forwarded[0]["port"] == 8102   # VL 候选，不是 p1 文本候选

    def test_no_vision_request_pure_priority_vl_not_skipped(self, registry, forwarded):
        # 用户裁决：VL 排最前就全接 — 纯文本请求不跳过 VL 候选
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"].insert(0,
            {"id": "vlloc", "priority": 0, "kind": "local",
             "model_name": "qwen-vl", "declared_protocols": ["openai"]})
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active={"qwen-small", "qwen-vl"},
                    local_models={"qwen-small": _local_model(),
                                  "qwen-vl": _local_model(name="qwen-vl",
                                                          served="qwen-vl",
                                                          port=8102)},
                    model_dicts=[{"name": "qwen-small", "model_type": "llm"},
                                 {"name": "qwen-vl", "model_type": "vl"}])
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        assert len(forwarded) == 1
        assert forwarded[0]["port"] == 8102   # p1 = VL，纯文本也由它服务

    def test_no_ready_target_message_lists_readiness_reasons(self, registry):
        # 7.10.0：视觉不再是排除维度 — 503 no_ready_target 消息只列
        # 就绪类理由，不含 feature_unsupported / 视觉指引后缀
        cfg = _raw_config()
        cfg["pools"]["fast"]["candidates"].append(
            {"id": "vlloc", "priority": 2, "kind": "local",
             "model_name": "qwen-vl", "declared_protocols": ["openai"]})
        cfg["pools"]["fast"]["candidates"][1]["priority"] = 3
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        # 全部本地候选未激活 → no_ready_target
        pm = FakePM(registry, active=set(),
                    local_models={"qwen-small": _local_model()},
                    model_dicts=[{"name": "qwen-small", "model_type": "llm"},
                                 {"name": "qwen-vl", "model_type": "vl"}])
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ]}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True
        status, body, _ = h.sent[-1]
        assert status == 503
        assert body["status"] == "no_ready_target"
        msg = body["error"]
        assert "loc: local_not_ready" in msg, msg
        assert "vlloc: local_not_ready" in msg, msg
        assert "feature_unsupported" not in msg, msg
        assert "no vision-capable candidate in this pool" not in msg, msg

    def test_tools_request_routed_but_normalized(self, registry, forwarded):
        # 7.8.0：tools 是 agent 基础能力 — 空声明（默认）即可路由
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
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


class TestStreamUsageInjection:
    """7.10.0：与直连路径（chat_handlers PR-G2）对齐 — 流式请求透明注入
    stream_options.include_usage=true。OpenAI SSE 默认不回 usage，不注入
    则池流式流量 RequestLog token 记 0、dashboard 统计失真。"""

    def test_streaming_request_injects_include_usage(self, registry, forwarded):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True

        assert len(forwarded) == 1
        assert forwarded[0]["stream"] is True
        assert forwarded[0]["body"]["stream_options"] == {"include_usage": True}

    def test_non_streaming_request_not_injected(self, registry, forwarded):
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True

        assert len(forwarded) == 1
        assert "stream_options" not in forwarded[0]["body"]

    def test_client_stream_options_preserved(self, registry, forwarded):
        # 客户端已设 include_usage=False（明确不要 usage）→ setdefault 语义，
        # 不覆盖客户端显式选择 — 与直连路径行为一致
        pm = FakePM(registry, active={"qwen-small"},
                    local_models={"qwen-small": _local_model()})
        h = FakeHandler()
        data = {"model": "iff/fast", "stream": True,
                "stream_options": {"include_usage": False},
                "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True

        assert len(forwarded) == 1
        assert forwarded[0]["body"]["stream_options"] == {"include_usage": False}

    def test_streaming_cloud_candidate_also_injected(
            self, registry, monkeypatch):
        # 云候选同链路：注入发生在入口（选路前），云流式请求同样拿到
        # usage chunk — 与直连路径（注入先于云路由）对齐
        cloud_bodies = []

        def fake_cloud(handler, data, provider_cfg, cloud_model, protocol="openai",
                       original_model=None, suppress_client_errors=False, timeout=None):
            cloud_bodies.append(dict(data))
            return SimpleNamespace(status=200, usage={}, ttft_ms=None,
                                   duration_ms=1.0, error=None)

        monkeypatch.setattr(dispatch.forwarder, "forward_to_cloud", fake_cloud)
        cfg = _raw_config()
        cfg["pools"]["fast"]["allow_cross_location"] = True
        registry.update(cfg, expected_revision=registry.snapshot().revision)
        pm = FakePM(registry, active=set(), local_models={},
                    cloud_models={"prov/cloud-m": SimpleNamespace(
                        supports_vision=None)},
                    providers={"prov": SimpleNamespace(
                        openai_base="https://base/v1")})
        h = FakeHandler()
        data = {"model": "iff/fast", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}

        assert dispatch.handle_pool_chat(h, pm, data, "iff/fast") is True

        assert len(cloud_bodies) == 1
        assert cloud_bodies[0]["stream_options"] == {"include_usage": True}
