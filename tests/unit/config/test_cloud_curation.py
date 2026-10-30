"""
unit/config/test_cloud_curation.py — 云端 Provider 模型策展（Phase 1 白名单）

测试对象: CloudDiscovery 策展方法 + ProviderConfig.enabled_models 白名单语义
覆盖范围:
  - 默认白名单制：发现候选不入注册表（无 enabled_models / 未勾选）
  - enable/disable_model：白名单开关 + 注册表同步
  - add/remove_manual_model：手填模型（空 spec 复用 model_specs 管道）
  - get_candidates：发现候选快照（白名单过滤前的完整候选）
  - 持久化往返：_serialize_providers ↔ _load_config（enabled_models + 空 spec）
  - resolve_route：未勾选发现模型 → None（404 unknown_model 语义）
  - 兼容回归：discovery 关闭 + specs（baidu 形态）→ specs 恒可路由
"""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Set

import pytest
import yaml

from inferfabric.cloud_discovery import CloudDiscovery, CloudModel


def _write_yaml(path: Path, data: dict):
    with open(path, "w") as f:
        yaml.dump(data, f)


# ── Mock /models server（同 test_cloud_discovery.py 范式） ──

class _MockModelsHandler(BaseHTTPRequestHandler):
    MODELS_RESPONSE = {
        "data": [
            {"id": "deepseek-v4-flash", "object": "model"},
            {"id": "glm-5", "object": "model"},
            {"id": "qwen3.5-72b", "object": "model"},
            {"id": "internal-test-model", "object": "model"},
        ]
    }

    def do_GET(self):
        if self.path == "/models":
            body = json.dumps(self.MODELS_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def mock_port():
    server = HTTPServer(("127.0.0.1", 0), _MockModelsHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


def _provider_yaml(tmp_path: Path, mock_port, **extra) -> Path:
    provider = {
        "api_key": "sk-test",
        "openai_base": f"http://127.0.0.1:{mock_port}",
    }
    provider.update(extra)
    p = tmp_path / "cloud_provider.yaml"
    _write_yaml(p, {"providers": {"test-provider": provider}})
    return p


def _candidate_ids(cd: CloudDiscovery, provider: str = "test-provider") -> set[str]:
    return {c.model_id for c in cd.get_candidates(provider)}


class TestWhitelistDefault:
    """白名单制：发现候选默认不勾、不路由。"""

    def test_discovered_not_in_registry_by_default(self, tmp_path, mock_port):
        _provider_yaml(tmp_path, mock_port)
        cd = CloudDiscovery(tmp_path / "cloud_provider.yaml")
        cd.discover_all()
        # 未勾选 → 注册表（可路由集）为空
        assert "deepseek-v4-flash" not in cd.cloud_models
        assert "glm-5" not in cd.cloud_models
        assert len(cd.cloud_models) == 0
        # 候选快照保留上游全量（供面板勾选）
        assert _candidate_ids(cd) == {
            "deepseek-v4-flash", "glm-5", "qwen3.5-72b", "internal-test-model",
        }

    def test_enabled_models_gates_registry(self, tmp_path, mock_port):
        _provider_yaml(tmp_path, mock_port, enabled_models=["glm-5"])
        cd = CloudDiscovery(tmp_path / "cloud_provider.yaml")
        models = cd.discover_all()
        # 勾选项入注册表（双键），未勾选不入
        assert "glm-5" in models
        assert "test-provider/glm-5" in models
        assert "deepseek-v4-flash" not in models
        # 路由语义：勾选 → cloud:<provider>；未勾选 → None（404）
        assert cd.resolve_route("glm-5", local_models=set()) == "cloud:test-provider"
        assert cd.resolve_route("deepseek-v4-flash", local_models=set()) is None

    def test_include_pattern_still_filters_candidates(self, tmp_path, mock_port):
        _provider_yaml(
            tmp_path, mock_port,
            enabled_models=["glm-5"],
            discovery={"filter": {"include_pattern": "^(deepseek|glm).*"}},
        )
        cd = CloudDiscovery(tmp_path / "cloud_provider.yaml")
        cd.discover_all()
        # include_pattern 仍在发现侧预筛候选（正交于白名单）
        assert _candidate_ids(cd) == {"deepseek-v4-flash", "glm-5"}
        assert set(cd.cloud_models) == {"glm-5", "test-provider/glm-5"}


class TestCurationMethods:
    """enable/disable/add/remove 策展方法。"""

    def _cd(self, tmp_path, mock_port, **extra) -> CloudDiscovery:
        p = _provider_yaml(tmp_path, mock_port, **extra)
        return CloudDiscovery(p)

    def test_enable_model_registers_candidate(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.discover_all()
        cd.enable_model("test-provider", "deepseek-v4-flash")
        models = cd.cloud_models
        assert "deepseek-v4-flash" in models
        assert "test-provider/deepseek-v4-flash" in models
        assert "deepseek-v4-flash" in cd.providers["test-provider"].enabled_models
        # 未 enable 的候选仍不可路由
        assert cd.resolve_route("qwen3.5-72b", local_models=set()) is None

    def test_enable_model_persists(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.enable_model("test-provider", "glm-5")
        saved = cd._serialize_providers()
        assert saved["providers"]["test-provider"]["enabled_models"] == ["glm-5"]

    def test_disable_model_removes_from_registry(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port, enabled_models=["glm-5"])
        cd.discover_all()
        cd.disable_model("test-provider", "glm-5")
        assert "glm-5" not in cd.cloud_models
        assert cd.resolve_route("glm-5", local_models=set()) is None
        assert cd.providers["test-provider"].enabled_models == []

    def test_disable_model_keeps_spec_model_routable(self, tmp_path, mock_port):
        # 模型同时是 spec（独立授权）：取消发现侧勾选不影响 spec 路由
        cd = self._cd(tmp_path, mock_port, enabled_models=["glm-5"],
                      models={"glm-5": {"context_window": 128000}})
        cd.discover_all()
        cd.disable_model("test-provider", "glm-5")
        assert "glm-5" in cd.cloud_models  # spec 恒注册
        assert cd.resolve_route("glm-5", local_models=set()) == "cloud:test-provider"

    def test_add_manual_model(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.add_manual_model("test-provider", "my-custom-glm")
        # 空 spec 复用 model_specs 管道 → spec-only 注册（discovered_at=0）
        m = cd.cloud_models.get("my-custom-glm")
        assert m is not None
        assert m.discovered_at == 0.0
        assert m.provider == "test-provider"
        assert cd.resolve_route("my-custom-glm", local_models=set()) == "cloud:test-provider"
        # 持久化走现有 models: 段（空 spec 也落盘 model_id）
        saved = cd._serialize_providers()
        assert "my-custom-glm" in saved["providers"]["test-provider"]["models"]

    def test_add_manual_model_idempotent(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.add_manual_model("test-provider", "my-custom-glm")
        cd.add_manual_model("test-provider", "my-custom-glm")  # 重复添加不炸
        assert "my-custom-glm" in cd.cloud_models

    def test_remove_manual_model(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.add_manual_model("test-provider", "my-custom-glm")
        cd.remove_manual_model("test-provider", "my-custom-glm")
        assert "my-custom-glm" not in cd.cloud_models
        assert "my-custom-glm" not in cd.providers["test-provider"].model_specs
        assert cd.resolve_route("my-custom-glm", local_models=set()) is None

    def test_remove_manual_model_not_found_raises(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        with pytest.raises(KeyError):
            cd.remove_manual_model("test-provider", "never-added")

    def test_unknown_provider_raises(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        with pytest.raises(KeyError):
            cd.enable_model("ghost-provider", "glm-5")
        with pytest.raises(KeyError):
            cd.disable_model("ghost-provider", "glm-5")
        with pytest.raises(KeyError):
            cd.add_manual_model("ghost-provider", "glm-5")
        with pytest.raises(KeyError):
            cd.remove_manual_model("ghost-provider", "glm-5")

    def test_drop_provider(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port, models={"spec-a": {"context_window": 4096}})
        cd.add_manual_model("test-provider", "my-custom-glm")
        cd.discover_all()
        cd.drop_provider("test-provider")
        assert "test-provider" not in cd.providers
        assert "my-custom-glm" not in cd.cloud_models
        assert "spec-a" not in cd.cloud_models
        assert cd.get_candidates("test-provider") == []


class TestCapabilityTagging:
    """模态/能力打标（L2b）：update_model_spec 写 model_specs → 注册表 → 持久化。

    云模型能力上游 /models 不提供（_discover_provider 只取 id），必须人工
    声明。model_specs 是唯一注入点（_merge_model_spec），打标即写 spec。
    """

    _cd = TestCurationMethods._cd

    def test_tag_vision_on_manual_model(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.add_manual_model("test-provider", "my-custom-glm")
        count = cd.update_model_spec("test-provider", "my-custom-glm",
                                     {"supports_vision": True})
        m = cd.cloud_models.get("my-custom-glm")
        assert m is not None
        assert m.supports_vision is True
        assert count == 1

    def test_tag_creates_spec_for_new_model(self, tmp_path, mock_port):
        # 打标未知模型 = 声明：创建 spec（恒可路由语义，与手填同管道）
        cd = self._cd(tmp_path, mock_port)
        cd.update_model_spec("test-provider", "brand-new-model",
                             {"supports_vision": True})
        assert cd.providers["test-provider"].model_specs["brand-new-model"] == {
            "supports_vision": True}
        assert cd.cloud_models["brand-new-model"].supports_vision is True

    def test_tag_merges_into_enabled_candidate(self, tmp_path, mock_port):
        # 已勾选的发现候选：spec 经 _merge_model_spec 合并进候选
        cd = self._cd(tmp_path, mock_port, enabled_models=["glm-5"])
        cd.discover_all()
        cd.update_model_spec("test-provider", "glm-5", {"supports_tools": True})
        assert cd.cloud_models["glm-5"].supports_tools is True

    def test_tag_persists_roundtrip(self, tmp_path, mock_port):
        # 序列化契约：supports_vision/supports_tools 必须落盘（此前在 known
        # 排除集里被静默丢弃 — 打标后任一策展操作 save_config 即丢失）
        cd = self._cd(tmp_path, mock_port)
        cd.update_model_spec("test-provider", "my-custom-glm",
                             {"supports_vision": True, "supports_tools": True})
        cd.save_config()
        cd2 = CloudDiscovery(tmp_path / "cloud_provider.yaml")
        m = cd2.cloud_models.get("my-custom-glm")
        assert m is not None
        assert m.supports_vision is True
        assert m.supports_tools is True

    def test_tag_off_persists_false(self, tmp_path, mock_port):
        # 关闭标签：显式 False 落盘，重载后不复活
        cd = self._cd(tmp_path, mock_port)
        cd.update_model_spec("test-provider", "my-custom-glm",
                             {"supports_vision": True})
        cd.update_model_spec("test-provider", "my-custom-glm",
                             {"supports_vision": False})
        cd.save_config()
        cd2 = CloudDiscovery(tmp_path / "cloud_provider.yaml")
        assert cd2.cloud_models["my-custom-glm"].supports_vision is False

    def test_add_manual_model_with_caps(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        cd.add_manual_model("test-provider", "my-custom-glm",
                            caps={"supports_vision": True})
        assert cd.cloud_models["my-custom-glm"].supports_vision is True
        saved = cd._serialize_providers()
        assert saved["providers"]["test-provider"]["models"]["my-custom-glm"][
            "supports_vision"] is True

    def test_tag_unknown_provider_raises(self, tmp_path, mock_port):
        cd = self._cd(tmp_path, mock_port)
        with pytest.raises(KeyError):
            cd.update_model_spec("ghost-provider", "glm-5", {"supports_vision": True})

    def test_tag_unknown_capability_ignored(self, tmp_path, mock_port):
        # 契约：只接受 supports_vision/supports_tools（handler 已校验；
        # 方法层防御性丢弃未知键，不写脏 spec）
        cd = self._cd(tmp_path, mock_port)
        cd.update_model_spec("test-provider", "my-custom-glm",
                             {"supports_vision": True, "evil_key": "x"})
        spec = cd.providers["test-provider"].model_specs["my-custom-glm"]
        assert "evil_key" not in spec


class TestPersistence:
    """enabled_models + 空 spec 手填模型的 YAML 往返。"""

    def test_enabled_models_roundtrip(self, tmp_path, mock_port):
        p = _provider_yaml(tmp_path, mock_port, enabled_models=["glm-5", "deepseek-v4-flash"])
        cd = CloudDiscovery(p)
        assert cd.providers["test-provider"].enabled_models == ["glm-5", "deepseek-v4-flash"]
        saved = cd._serialize_providers()
        assert saved["providers"]["test-provider"]["enabled_models"] == \
            ["glm-5", "deepseek-v4-flash"]

    def test_empty_enabled_models_not_serialized(self, tmp_path, mock_port):
        _provider_yaml(tmp_path, mock_port)
        cd = CloudDiscovery(tmp_path / "cloud_provider.yaml")
        saved = cd._serialize_providers()
        assert "enabled_models" not in saved["providers"]["test-provider"]

    def test_manual_empty_spec_roundtrip(self, tmp_path, mock_port):
        p = _provider_yaml(tmp_path, mock_port)
        cd = CloudDiscovery(p)
        cd.add_manual_model("test-provider", "my-custom-glm")
        cd.save_config()
        # 重新加载 → 手填模型仍在 model_specs，且恒可路由
        cd2 = CloudDiscovery(p)
        assert "my-custom-glm" in cd2.providers["test-provider"].model_specs
        assert "my-custom-glm" in cd2.cloud_models


class TestCompatibility:
    """baidu 形态（discovery 关 + specs）：specs 恒可路由，零变化。"""

    def test_discovery_off_specs_always_routable(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "baidu-codingplan": {
                    "api_key": "${IFF_BAIDU_CODINGPLAN_KEY}",
                    "openai_base": "https://qianfan.baidubce.com/v2/coding",
                    "anthropic_base": "https://qianfan.baidubce.com/anthropic/coding/v1",
                    "discovery": {"enabled": False},
                    "models": {
                        "glm-5": {"price_input": 0.5, "price_output": 0.5},
                        "glm-5.1": {"price_input": 0.5, "price_output": 0.5},
                        "deepseek-v4-flash": {"price_input": 1.0, "price_output": 2.0},
                        "deepseek-v4-pro": {"price_input": 4.0, "price_output": 16.0},
                    },
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        for mid in ("glm-5", "glm-5.1", "deepseek-v4-flash", "deepseek-v4-pro"):
            assert mid in models, f"{mid} 应恒可路由（spec）"
            assert cd.resolve_route(mid, local_models=set()) == "cloud:baidu-codingplan"
        # 无发现候选（discovery 关）
        assert cd.get_candidates("baidu-codingplan") == []
