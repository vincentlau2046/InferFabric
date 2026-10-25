"""
unit/config/test_cloud_discovery.py — 云服务发现测试

测试对象: inferfabric.cloud_discovery.CloudDiscovery, ProviderConfig, CloudModel
覆盖范围:
  - ProviderConfig 默认值
  - 配置加载（无配置、空配置、单 provider、disabled provider）
  - 模型发现（带 filter、无 filter、Anthropic 标记、provider 不可达）
  - 路由决策（local 优先、cloud、provider 前缀、无匹配）
"""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
import time

import pytest
import yaml

from inferfabric.cloud_discovery import CloudDiscovery, CloudModel, ProviderConfig


def _write_yaml(path: Path, data: dict):
    with open(path, "w") as f:
        yaml.dump(data, f)


# ── Mock HTTP server for /models ──

class _MockModelsHandler(BaseHTTPRequestHandler):
    """Returns a canned /models response."""
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
        pass  # suppress logs


@pytest.fixture(scope="module")
def mock_server():
    """Start a mock /models HTTP server on a free port."""
    server = HTTPServer(("127.0.0.1", 0), _MockModelsHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()


class TestProviderConfig:
    """ProviderConfig dataclass defaults"""

    def test_defaults(self):
        cfg = ProviderConfig(name="test")
        assert cfg.enabled
        assert cfg.discovery_enabled
        assert cfg.discovery_interval == 3600
        assert cfg.routing_default == "cloud_only"
        assert cfg.timeout == 600  # R3: 60s→600s

    def test_default_timeout_is_600(self):
        """默认 timeout 为 600s（R3）。"""
        cfg = ProviderConfig(name="test")
        assert cfg.timeout == 600

    def test_default_timeout_not_60(self):
        """确保不再是旧的 60s 默认值。"""
        cfg = ProviderConfig(name="test")
        assert cfg.timeout != 60


class TestCloudDiscoveryConfig:
    """配置加载"""

    def test_no_config(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "nonexistent.yaml")
        assert len(cd.providers) == 0

    def test_empty_config(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        p.write_text("")
        cd = CloudDiscovery(p)
        assert len(cd.providers) == 0

    def test_load_single_provider(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "baidu-codingplan": {
                    "api_key": "sk-test",
                    "openai_base": "https://example.com/v1",
                    "anthropic_base": "https://example.com/anthropic/v1",
                    "timeout": 30,
                    "enabled": True,
                    "discovery": {
                        "enabled": True,
                        "endpoint": "/models",
                        "interval": 1800,
                        "filter": {"include_pattern": "^(deepseek|glm).*"},
                    },
                    "routing": {"default": "cloud_only"},
                }
            }
        })
        cd = CloudDiscovery(p)
        assert "baidu-codingplan" in cd.providers
        cfg = cd.providers["baidu-codingplan"]
        assert cfg.api_key == "sk-test"
        assert cfg.include_pattern == "^(deepseek|glm).*"
        assert cfg.discovery_interval == 1800

    def test_load_disabled_provider(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "disabled-one": {"enabled": False, "openai_base": "https://x.com"},
            }
        })
        cd = CloudDiscovery(p)
        assert "disabled-one" in cd.providers
        assert not cd.providers["disabled-one"].enabled


class TestCloudDiscoveryDiscover:
    """模型发现"""

    def test_discover_with_filter(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "anthropic_base": f"http://127.0.0.1:{mock_server}/anthropic",
                    "enabled_models": ["deepseek-v4-flash", "glm-5"],
                    "discovery": {
                        "filter": {"include_pattern": "^(deepseek|glm).*"},
                    },
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        # Should include deepseek-v4-flash and glm-5, but not qwen3.5-72b or internal-test-model
        assert "deepseek-v4-flash" in models
        assert "glm-5" in models
        assert "qwen3.5-72b" not in models
        assert "internal-test-model" not in models
        # include_pattern 仍在发现侧预筛候选（白名单正交）
        cands = {c.model_id for c in cd.get_candidates("test-provider")}
        assert cands == {"deepseek-v4-flash", "glm-5"}

    def test_discover_no_filter(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "enabled_models": ["deepseek-v4-flash", "glm-5", "qwen3.5-72b", "internal-test-model"],
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        # 4 short-name keys + 4 provider-prefixed keys = 8
        assert len(models) == 8
        # 全部模型同时进入候选快照（发现无过滤）
        cands = {c.model_id for c in cd.get_candidates("test-provider")}
        assert cands == {"deepseek-v4-flash", "glm-5", "qwen3.5-72b", "internal-test-model"}
        # All 4 unique model IDs exist
        model_ids = {m.model_id for m in models.values()}
        assert model_ids == {"deepseek-v4-flash", "glm-5", "qwen3.5-72b", "internal-test-model"}

    def test_discover_anthropic_flag(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "anthropic_base": f"http://127.0.0.1:{mock_server}/anthropic",
                    "enabled_models": ["deepseek-v4-flash", "glm-5", "qwen3.5-72b", "internal-test-model"],
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        for m in models.values():
            assert m.anthropic_available is True

    def test_discover_no_anthropic_base(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "test-provider": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    # no anthropic_base
                }
            }
        })
        cd = CloudDiscovery(p)
        cd.discover_all()
        # v6.1 白名单制：无 enabled_models → 注册表为空；候选快照保留并标记协议
        assert len(cd.cloud_models) == 0
        cands = cd.get_candidates("test-provider")
        assert len(cands) == 4
        for m in cands:
            assert m.anthropic_available is False
        # 勾选后注册表出现，且 anthropic_available 仍为 False
        cd.enable_model("test-provider", "deepseek-v4-flash")
        assert cd.cloud_models["deepseek-v4-flash"].anthropic_available is False

    def test_discover_provider_down(self, tmp_path):
        """Provider 不可达时返回空列表，不抛异常。"""
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "dead-provider": {
                    "api_key": "sk-test",
                    "openai_base": "http://127.0.0.1:1",  # unreachable
                    "timeout": 1,
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) == 0

    def test_discover_disabled_provider_skipped(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "off": {
                    "enabled": False,
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) == 0


class TestCloudDiscoveryRoute:
    """路由决策"""

    def test_route_local(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "deepseek-v4-flash": CloudModel("deepseek-v4-flash", "baidu-codingplan"),
        }
        result = cd.resolve_route("qwen36-35b-vl", local_models={"qwen36-35b-vl"})
        assert result == "local"

    def test_route_cloud(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "deepseek-v4-flash": CloudModel("deepseek-v4-flash", "baidu-codingplan"),
        }
        result = cd.resolve_route("deepseek-v4-flash", local_models=set())
        assert result == "cloud:baidu-codingplan"

    def test_route_with_provider_prefix(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "glm-5": CloudModel("glm-5", "baidu-codingplan"),
        }
        result = cd.resolve_route("baidu-codingplan/glm-5", local_models=set())
        assert result == "cloud:baidu-codingplan"

    def test_route_no_match(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {}
        result = cd.resolve_route("unknown-model", local_models=set())
        assert result is None

    def test_route_local_takes_priority(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cd._cloud_models = {
            "qwen36-35b-vl": CloudModel("qwen36-35b-vl", "baidu-codingplan"),
        }
        result = cd.resolve_route("qwen36-35b-vl", local_models={"qwen36-35b-vl"})
        assert result == "local"


class TestSpecAliasAndDiscoveryUA:
    """v6.6: model_specs 别名语义 + 发现请求 User-Agent。

    别名语义（与 _serialize_providers 的写盘格式一致）：
      yaml: models: {k3: {model_id: kimi/k3}}
      → 注册表 key = "k3"（短名，routing 按 split("/")[-1] 查表）
      → CloudModel.model_id = "kimi/k3"（上游真实 id，转发时覆盖 data["model"]）

    回归背景：spec["model_id"] 曾只落进 extra（不参与注册/转发），
    导致 kimi/k3 这类含 "/" 的上游模型必然 404/403。
    """

    def _cfg(self, tmp_path):
        path = tmp_path / "cloud_provider.yaml"
        _write_yaml(path, {
            "providers": {
                "tathr": {
                    "openai_base": "https://api.tathr.com/v1",
                    "anthropic_base": "https://api.tathr.com/v1",
                    "api_key": "test-key",
                    "discovery": {"enabled": False},
                    "models": {
                        "gpt-6.1-sol": {"model_id": "gpt-6.1-sol"},
                        "k3": {"model_id": "kimi/k3", "context_window": 128000},
                    },
                }
            }
        })
        return CloudDiscovery(path)

    def test_alias_registers_short_key_with_upstream_model_id(self, tmp_path):
        reg = self._cfg(tmp_path).discover_all()
        assert "k3" in reg and "tathr/k3" in reg
        assert reg["k3"].model_id == "kimi/k3"
        assert reg["tathr/k3"].model_id == "kimi/k3"
        # 无别名（key == model_id）时行为不变
        assert reg["gpt-6.1-sol"].model_id == "gpt-6.1-sol"

    def test_alias_merges_spec_attrs_via_spec_key(self, tmp_path):
        reg = self._cfg(tmp_path).discover_all()
        assert reg["k3"].context_window == 128000

    def test_alias_routable_by_full_name_short_name_and_prefixed(self, tmp_path):
        cd = self._cfg(tmp_path)
        cd.discover_all()
        assert cd.resolve_route("kimi/k3", set()) == "cloud:tathr"
        assert cd.resolve_route("k3", set()) == "cloud:tathr"
        assert cd.resolve_route("tathr/k3", set()) == "cloud:tathr"

    def test_discovery_request_carries_explicit_user_agent(self, tmp_path, monkeypatch):
        """urllib 默认 "Python-urllib/3.x" 会被 Cloudflare 403 (error 1010)。"""
        from inferfabric import cloud_discovery as cdm
        captured = {}

        class _Resp:
            def read(self):
                return b'{"data": [{"id": "kimi/k3"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def _fake_urlopen(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            return _Resp()

        monkeypatch.setattr(cdm.urllib.request, "urlopen", _fake_urlopen)
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cfg = ProviderConfig(
            name="tathr",
            api_key="k",
            openai_base="https://api.tathr.com/v1",
            anthropic_base="https://api.tathr.com/v1",
        )
        models = cd._discover_provider(cfg)
        assert [m.model_id for m in models] == ["kimi/k3"]
        assert captured["ua"] == cdm._CLOUD_UA
