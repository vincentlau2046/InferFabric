"""
unit/proxy/test_provider_models.py — v6.1 Phase 1: POST /admin/cloud/provider-models

模型策展 API：enable/disable/add/remove 四动作 + 错误路径（404/400）+ 持久化。
参照 test_post_provider_key_expand.py 的 handler 测试范式（__new__ + _send_json stub）。
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_deps = _ROOT / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

from inferfabric.cloud_discovery import CloudDiscovery, CloudModel, SecretsManager
from inferfabric.proxy.handler import ProxyHandler


def _make_handler(command: str, body: dict):
    h = ProxyHandler.__new__(ProxyHandler)
    h.command = command
    h.path = "/admin/cloud/provider-models"
    h.headers = {}
    h._read_body = lambda: body
    h._sent = []

    def _send_json(data, status=200, extra_headers=None):
        h._sent.append((data, status))

    h._send_json = _send_json
    return h


def _make_pm(tmp_path):
    """真实 CloudDiscovery（tmp 路径）+ 预置 provider + 候选快照。"""
    cfg_path = tmp_path / "cloud_provider.yaml"
    secrets_path = tmp_path / "secrets.env"

    cd = CloudDiscovery(config_path=None)
    cd._config_path = cfg_path
    cd._secrets_mgr = SecretsManager(path=secrets_path)
    cd._inject_secrets_env = lambda: None

    from inferfabric.cloud_discovery import ProviderConfig
    cd._providers["openai-x"] = ProviderConfig(
        name="openai-x",
        api_key="sk-real-123",
        openai_base="https://api.x.com/v1",
        enabled=True,
        discovery_enabled=True,
        key_env_var="IFF_OPENAI_X_KEY",
    )
    # 候选快照（仿一次已发生的发现）
    cd._candidates["openai-x"] = [
        CloudModel(model_id="glm-5", provider="openai-x", discovered_at=time.time()),
        CloudModel(model_id="deepseek-v4-flash", provider="openai-x", discovered_at=time.time()),
    ]

    pm = SimpleNamespace(cloud=cd)
    return pm, cd, cfg_path


def _last_sent(h):
    assert h._sent, "handler did not send a response"
    return h._sent[-1]


class TestProviderModels:
    """四动作核心路径。"""

    def test_enable_routes_candidate(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "enable", "model": "glm-5"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        assert data["status"] == "ok"
        assert data["routable_count"] == 1
        # 白名单 + 注册表 + 路由
        assert cd.providers["openai-x"].enabled_models == ["glm-5"]
        assert "glm-5" in cd.cloud_models
        assert cd.resolve_route("glm-5", local_models=set()) == "cloud:openai-x"
        # 持久化
        assert cfg.exists()
        saved = yaml.safe_load(cfg.read_text())
        assert saved["providers"]["openai-x"]["enabled_models"] == ["glm-5"]

    def test_disable_unroutes(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        cd.providers["openai-x"].enabled_models.append("glm-5")
        cd._rebuild_provider_registry("openai-x", cd.providers["openai-x"])
        h = _make_handler("POST", {"provider": "openai-x", "action": "disable", "model": "glm-5"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        assert data["routable_count"] == 0
        assert "glm-5" not in cd.cloud_models
        assert cd.resolve_route("glm-5", local_models=set()) is None  # → 404 unknown_model

    def test_add_manual_model(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "add", "model": "my-custom-glm"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        assert data["routable_count"] == 1
        assert "my-custom-glm" in cd.providers["openai-x"].model_specs
        assert cd.resolve_route("my-custom-glm", local_models=set()) == "cloud:openai-x"
        saved = yaml.safe_load(cfg.read_text())
        assert "my-custom-glm" in saved["providers"]["openai-x"]["models"]

    def test_remove_manual_model(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        cd.add_manual_model("openai-x", "my-custom-glm")
        h = _make_handler("POST", {"provider": "openai-x", "action": "remove", "model": "my-custom-glm"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        assert "my-custom-glm" not in cd.cloud_models
        assert cd.resolve_route("my-custom-glm", local_models=set()) is None


class TestProviderModelsTag:
    """tag 动作 + add 带能力标签（L2b）：{supports_vision, supports_tools} → spec。"""

    def test_tag_sets_capability(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "tag",
                                   "model": "glm-5", "supports_vision": True})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        assert data["action"] == "tag"
        # spec 写入 + 注册表可见 + 持久化
        assert cd.providers["openai-x"].model_specs["glm-5"]["supports_vision"] is True
        assert cd.cloud_models["glm-5"].supports_vision is True
        saved = yaml.safe_load(cfg.read_text())
        assert saved["providers"]["openai-x"]["models"]["glm-5"]["supports_vision"] is True

    def test_tag_off_capability_unsets_not_constrains(self, tmp_path):
        """7.8.0：面板 chip 关闭 = 解除打标（键移除），不得持久化 false。

        显式 false 自 7.8.0 起构成池候选活跃约束（catalog 排除词表）——
        若 tag off 写 false，用户在云面板点一次 chip 就能造出「池候选拒
        tools 请求」且 UI 无回路（cross-review I-1）。
        """
        pm, cd, cfg = _make_pm(tmp_path)
        cd.update_model_spec("openai-x", "glm-5", {"supports_vision": True})
        h = _make_handler("POST", {"provider": "openai-x", "action": "tag",
                                   "model": "glm-5", "supports_vision": False})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        # 键移除 → 三态回 None（无约束），注册表/YAML 一致
        assert "supports_vision" not in \
            cd.providers["openai-x"].model_specs["glm-5"]
        assert cd.cloud_models["glm-5"].supports_vision is None
        saved = yaml.safe_load(cfg.read_text())
        assert "supports_vision" not in \
            saved["providers"]["openai-x"]["models"]["glm-5"]

    def test_tag_off_lifts_explicit_false_constraint(self, tmp_path):
        """YAML 显式 false（约束）可被面板一次点击解除（tag true 覆盖，
        再 tag off 回 unset — 双向可逆，无单向陷阱）。"""
        pm, cd, cfg = _make_pm(tmp_path)
        cd.add_manual_model("openai-x", "glm-5",
                            caps={"supports_tools": False})
        assert cd.cloud_models["glm-5"].supports_tools is False
        h = _make_handler("POST", {"provider": "openai-x", "action": "tag",
                                   "model": "glm-5", "supports_tools": False})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        assert cd.cloud_models["glm-5"].supports_tools is None
        saved = yaml.safe_load(cfg.read_text())
        assert "supports_tools" not in \
            saved["providers"]["openai-x"]["models"]["glm-5"]

    def test_add_with_caps(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "add",
                                   "model": "my-vlm", "supports_vision": True,
                                   "supports_tools": True})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 200, data
        m = cd.cloud_models["my-vlm"]
        assert m.supports_vision is True and m.supports_tools is True
        saved = yaml.safe_load(cfg.read_text())
        spec = saved["providers"]["openai-x"]["models"]["my-vlm"]
        assert spec["supports_vision"] is True and spec["supports_tools"] is True

    def test_tag_without_caps_400(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "tag",
                                   "model": "glm-5"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 400
        # 拒绝时不留半截 spec
        assert "glm-5" not in cd.providers["openai-x"].model_specs

    def test_tag_non_bool_value_400(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "tag",
                                   "model": "glm-5", "supports_vision": "yes"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 400

    def test_tag_unknown_provider_404(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "ghost", "action": "tag",
                                   "model": "glm-5", "supports_vision": True})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 404


class TestProviderModelsGET:
    """GET /admin/cloud/providers 契约：策展字段暴露给前端。"""

    def test_get_exposes_curation_fields(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        cd.enable_model("openai-x", "glm-5")
        cd.add_manual_model("openai-x", "my-custom-glm")
        h = _make_handler("GET", {})
        h.path = "/admin/cloud/providers"
        h._handle_cloud_providers(pm)
        data, status = _last_sent(h)
        assert status == 200
        p = next(x for x in data["providers"] if x["name"] == "openai-x")
        assert p["enabled_models"] == ["glm-5"]
        # 手填空 spec → manual=True；带元数据 spec → manual=False。
        # supports_vision/supports_tools 恒输出（三态：未声明 = None，
        # 7.8.0 cross-review I-1）— 前端 chips 直接读，不用猜键是否存在
        assert p["model_specs"] == [{"id": "my-custom-glm", "manual": True,
                                     "supports_vision": None, "supports_tools": None}]
        assert set(p["candidates"]) == {"glm-5", "deepseek-v4-flash"}
        assert p["routable_count"] == 2
        # 顶层 models 列表 = 可路由集（短名去重）
        ids = {m["id"] for m in data["models"]}
        assert ids == {"glm-5", "my-custom-glm"}

    def test_get_exposes_spec_capabilities(self, tmp_path):
        # 打标后的 spec 能力必须暴露给前端（chips 状态源）
        pm, cd, cfg = _make_pm(tmp_path)
        cd.add_manual_model("openai-x", "my-vlm")
        cd.update_model_spec("openai-x", "my-vlm", {"supports_vision": True})
        h = _make_handler("GET", {})
        h.path = "/admin/cloud/providers"
        h._handle_cloud_providers(pm)
        data, status = _last_sent(h)
        assert status == 200
        p = next(x for x in data["providers"] if x["name"] == "openai-x")
        spec = next(s for s in p["model_specs"] if s["id"] == "my-vlm")
        assert spec["supports_vision"] is True
        # 7.8.0 三态透传：未声明 = None（JSON null），不得 bool() 塌缩成
        # false — 前端须能区分「无约束」与「显式排除」（cross-review I-1）
        assert spec["supports_tools"] is None

    def test_get_distinguishes_explicit_false_from_unset(self, tmp_path):
        """显式 false（YAML 约束）与未声明在 GET 中必须可区分。"""
        pm, cd, cfg = _make_pm(tmp_path)
        cd.add_manual_model("openai-x", "constrained-m",
                            caps={"supports_tools": False})
        cd.add_manual_model("openai-x", "untagged-m")
        h = _make_handler("GET", {})
        h.path = "/admin/cloud/providers"
        h._handle_cloud_providers(pm)
        data, status = _last_sent(h)
        assert status == 200
        p = next(x for x in data["providers"] if x["name"] == "openai-x")
        specs = {s["id"]: s for s in p["model_specs"]}
        assert specs["constrained-m"]["supports_tools"] is False
        assert specs["untagged-m"]["supports_tools"] is None

    def test_get_exposes_routable_caps(self, tmp_path):
        # 候选行 chips 状态源：已入注册表（可路由）的候选带能力快照；
        # 未勾选候选不在 map（前端不给未路由候选渲染 chips — 打标即声明
        # 会使其恒可路由，避免意外路由）
        pm, cd, cfg = _make_pm(tmp_path)
        cd.enable_model("openai-x", "glm-5")
        cd.update_model_spec("openai-x", "glm-5", {"supports_tools": True})
        h = _make_handler("GET", {})
        h.path = "/admin/cloud/providers"
        h._handle_cloud_providers(pm)
        data, status = _last_sent(h)
        assert status == 200
        p = next(x for x in data["providers"] if x["name"] == "openai-x")
        # 三态透传：未声明 = None（cross-review I-1）
        assert p["routable_caps"]["glm-5"] == {"supports_vision": None,
                                               "supports_tools": True}
        assert "deepseek-v4-flash" not in p["routable_caps"]


class TestProviderModelsErrors:
    """错误路径：404 / 400。"""

    def test_unknown_provider_404(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "ghost", "action": "enable", "model": "glm-5"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 404
        assert "ghost" in data["error"]

    def test_missing_model_400(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "enable"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 400

    def test_invalid_action_400(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "explode", "model": "x"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 400

    def test_remove_unknown_manual_404(self, tmp_path):
        pm, cd, cfg = _make_pm(tmp_path)
        h = _make_handler("POST", {"provider": "openai-x", "action": "remove", "model": "never-added"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 404
        assert "never-added" in data["error"]


class TestProviderModelsPersistenceFailure:
    """save_config 失败 → 500，不静默。"""

    def test_save_failure_500(self, tmp_path, monkeypatch):
        pm, cd, cfg = _make_pm(tmp_path)

        import inferfabric.cloud_discovery as cdm
        def _boom(self):
            raise OSError("disk full")
        monkeypatch.setattr(cdm.CloudDiscovery, "save_config", _boom)

        h = _make_handler("POST", {"provider": "openai-x", "action": "enable", "model": "glm-5"})
        h._handle_cloud_provider_models(pm)
        data, status = _last_sent(h)
        assert status == 500