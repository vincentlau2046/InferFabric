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


class TestDiscoverProviderFailureContract:
    """7.9.1 floor 前置契约：_discover_provider 三态化。

    抓取失败（网络/HTTP/解析异常）→ None（与「上游确实无模型 []」严格区分，
    调用方 discover_all 据 None 走「保留 floor」而非「0 模型」路径）。
    无 openai_base → []（永久配置状态，非瞬时失败，不刷告警）。
    """

    def _cfg(self, **overrides):
        base = dict(
            name="prov",
            api_key="k",
            openai_base="https://api.example.com/v1",
        )
        base.update(overrides)
        return ProviderConfig(**base)

    def test_discover_provider_none_on_timeout(self, tmp_path, monkeypatch):
        import urllib.error
        from inferfabric import cloud_discovery as cdm

        def _fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr(cdm.urllib.request, "urlopen", _fake_urlopen)
        cd = CloudDiscovery(tmp_path / "none.yaml")
        assert cd._discover_provider(self._cfg()) is None

    def test_discover_provider_none_on_http_error(self, tmp_path, monkeypatch):
        import urllib.error
        from inferfabric import cloud_discovery as cdm

        def _fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 500, "Internal Server Error", None, None)

        monkeypatch.setattr(cdm.urllib.request, "urlopen", _fake_urlopen)
        cd = CloudDiscovery(tmp_path / "none.yaml")
        assert cd._discover_provider(self._cfg()) is None

    def test_discover_provider_none_on_malformed_json(self, tmp_path, monkeypatch):
        import urllib.error
        from inferfabric import cloud_discovery as cdm

        class _Resp:
            def read(self):
                return b"not-json{"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(cdm.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        cd = CloudDiscovery(tmp_path / "none.yaml")
        # 无法解析 ≠ 上游 0 模型 — 同样视为抓取失败
        assert cd._discover_provider(self._cfg()) is None

    def test_discover_provider_empty_list_on_success_empty(self, tmp_path, monkeypatch):
        from inferfabric import cloud_discovery as cdm

        class _Resp:
            def read(self):
                return b'{"data": []}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(cdm.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        cd = CloudDiscovery(tmp_path / "none.yaml")
        result = cd._discover_provider(self._cfg())
        assert result == []  # 成功空 = 上游真相，与 None（失败）可区分

    def test_discover_provider_no_openai_base_returns_empty_list(self, tmp_path):
        cd = CloudDiscovery(tmp_path / "none.yaml")
        cfg = self._cfg(openai_base="")
        # 永久配置状态（未配置 openai base）→ 空 list，不触发失败告警
        assert cd._discover_provider(cfg) == []


class TestFetchFailureFloor:
    """7.9.1 P0 事故修复：抓取失败不逐出白名单模型（floor = specs ∪ 白名单）。

    事故复现（2026-11-01 生产）：tathr-glm /models 600s 超时 → discover_all
    整表替换把白名单模型 glm-5 静默逐出 → 404 直到手动「模型发现」。
    floor 语义：单一 provider 抓取失败只减抓取贡献，不减 floor。
    """

    def _write_cfg(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "tathr-glm": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "enabled_models": ["glm-5"],
                },
                "specprov": {
                    "discovery": {"enabled": False},
                    "openai_base": "https://api.example.com/v1",
                    "models": {"x1": {"model_id": "x1"}},
                },
            }
        })
        return CloudDiscovery(p)

    def _fail_urlopen(self, monkeypatch):
        import urllib.error
        from inferfabric import cloud_discovery as cdm

        def _fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("the handshake operation timed out")

        monkeypatch.setattr(cdm.urllib.request, "urlopen", _fake_urlopen)

    def test_fetch_failure_retains_whitelist_models(self, tmp_path, mock_server, monkeypatch):
        cd = self._write_cfg(tmp_path, mock_server)
        # 第一次发现成功（mock server 返回 glm-5，在白名单内）
        reg = cd.discover_all()
        assert "glm-5" in reg
        assert cd.resolve_route("glm-5", set()) == "cloud:tathr-glm"

        # 第二轮：抓取失败 — 事故复现点
        self._fail_urlopen(monkeypatch)
        reg2 = cd.discover_all()
        # 白名单模型保留（RED：现状被整表替换逐出）
        assert "glm-5" in reg2
        assert "tathr-glm/glm-5" in reg2
        assert cd.resolve_route("glm-5", set()) == "cloud:tathr-glm"
        # 其他 provider 的 spec 模型完好
        assert "x1" in reg2 and "specprov/x1" in reg2
        assert cd.cloud_models["glm-5"].provider == "tathr-glm"

    def test_fetch_failure_retains_old_metadata(self, tmp_path, mock_server, monkeypatch):
        cd = self._write_cfg(tmp_path, mock_server)
        cd.discover_all()  # 成功发现 → glm-5 discovered_at > 0
        assert cd.cloud_models["glm-5"].discovered_at > 0

        self._fail_urlopen(monkeypatch)
        reg2 = cd.discover_all()
        # 保留的是旧条目（带元数据），不是裸重建
        assert reg2["glm-5"].discovered_at > 0

    def test_fetch_failure_logs_retained_count(self, tmp_path, mock_server, monkeypatch, caplog):
        import logging
        cd = self._write_cfg(tmp_path, mock_server)
        cd.discover_all()
        self._fail_urlopen(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="inferfabric.cloud_discovery"):
            cd.discover_all()
        msgs = [r.getMessage() for r in caplog.records]
        assert any("tathr-glm" in m and "1 whitelisted" in m for m in msgs), msgs

    def test_fetch_failure_no_floor_stays_empty(self, tmp_path, monkeypatch):
        """无白名单无 spec 的 provider 抓取失败 → 注册表为空（guard 语义不变）。"""
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "bare-provider": {
                    "api_key": "sk-test",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                }
            }
        })
        cd = CloudDiscovery(p)
        models = cd.discover_all()
        assert len(models) == 0


class TestShrunkenFetchFloor:
    """7.9.1 用户裁决（保留/白名单赢）：成功抓取但 /models 缩水（上游真删）
    也不驱逐白名单模型 — 失败以上游真实错误呈现；面板取消勾选是唯一移除途径。
    """

    def _write_cfg(self, tmp_path, mock_server):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "tathr-glm": {
                    "api_key": "sk-test",
                    "openai_base": f"http://127.0.0.1:{mock_server}",
                    "enabled_models": ["glm-5"],
                },
            }
        })
        return CloudDiscovery(p)

    def _shrunken_urlopen(self, monkeypatch):
        from inferfabric import cloud_discovery as cdm

        class _Resp:
            def read(self):
                return b'{"data": [{"id": "other-model"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(cdm.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())

    def test_whitelist_model_absent_from_successful_fetch_stays_routable(
            self, tmp_path, mock_server, monkeypatch):
        cd = self._write_cfg(tmp_path, mock_server)
        cd.discover_all()  # glm-5 在新鲜列表中
        assert cd.resolve_route("glm-5", set()) == "cloud:tathr-glm"

        # 成功抓取但列表缩水（glm-5 已不在 /models 中）
        self._shrunken_urlopen(monkeypatch)
        reg = cd.discover_all()
        assert "glm-5" in reg and "tathr-glm/glm-5" in reg
        assert cd.resolve_route("glm-5", set()) == "cloud:tathr-glm"
        # 保留旧条目元数据（非裸重建）
        assert reg["glm-5"].discovered_at > 0
        # 候选快照仍刷新为缩水集（面板如实反映上游现状）
        cands = cd.get_candidates("tathr-glm")
        assert [m.model_id for m in cands] == ["other-model"]

    def test_whitelist_absent_warning_logs_once_per_transition(
            self, tmp_path, mock_server, monkeypatch, caplog):
        import logging
        cd = self._write_cfg(tmp_path, mock_server)
        cd.discover_all()  # glm-5 在场

        def _absent_count():
            return sum(1 for r in caplog.records
                       if "kept routable" in r.getMessage()
                       and "glm-5" in r.getMessage())

        with caplog.at_level(logging.WARNING, logger="inferfabric.cloud_discovery"):
            self._shrunken_urlopen(monkeypatch)
            cd.discover_all()  # 第一次缺席
            assert _absent_count() == 1
            cd.discover_all()  # 连续缺席 — 不刷屏
            assert _absent_count() == 1
            # 模型回归 → 转态清除
            monkeypatch.undo()
            cd.discover_all()
            assert _absent_count() == 1
            # 再次缺席 → 第二次告警（新一轮转态）
            self._shrunken_urlopen(monkeypatch)
            cd.discover_all()
            assert _absent_count() == 2

    def test_no_openai_base_provider_skips_absent_warning(
            self, tmp_path, caplog):
        """cross-review M-3：无 openai_base（anthropic-only）provider 根本不抓取
        /models — 其白名单模型不应触发「absent from /models」误导性告警。
        """
        import logging
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "anthropic-only": {
                    "api_key": "sk-a",
                    "anthropic_base": "https://example.com/anthropic/v1",
                    "enabled_models": ["claude-x"],
                }
            }
        })
        cd = CloudDiscovery(p)
        with caplog.at_level(logging.WARNING, logger="inferfabric.cloud_discovery"):
            cd.discover_all()
        absent_warnings = [r for r in caplog.records
                           if "absent from /models" in r.getMessage()]
        assert not absent_warnings
        # floor 语义不变：仍可路由
        assert cd.resolve_route("claude-x", set()) == "cloud:anthropic-only"


class TestCloudReloadReconciliation:
    """7.9.1：reload() 调和注册表 + 重启轮询（SIGHUP 路径修复）。

    缺陷：reload() 重设 _providers 但不协调 _cloud_models/_candidates —
    已删除/禁用 provider 的旧条目滞留、新 provider 的 floor 不注册；
    且 SIGHUP 路径（config_reloader 只调 reload()）stop_polling 后无人
    重启轮询线程 — 轮询永久停摆直到进程重启。
    """

    def _yaml_v1(self, tmp_path):
        return {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",  # dead — 免抓取验证 floor
                    "timeout": 1,
                    "enabled_models": ["glm-5"],
                },
                "prov-b": {
                    "api_key": "sk-b",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["k3"],
                },
            }
        }

    def test_reload_drops_removed_provider_entries(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, self._yaml_v1(tmp_path))
        cd = CloudDiscovery(p)
        assert "glm-5" in cd.cloud_models and "k3" in cd.cloud_models

        # YAML 编辑：删除 prov-a（用户显式动作 — floor 允许移除）
        v2 = self._yaml_v1(tmp_path)
        del v2["providers"]["prov-a"]
        _write_yaml(p, v2)
        cd.reload()
        reg = cd.cloud_models
        assert "glm-5" not in reg and "prov-a/glm-5" not in reg
        assert "prov-a" not in cd._candidates
        # 另一 provider 的白名单模型 reload 后免抓取仍可路由
        assert "k3" in reg
        assert cd.resolve_route("k3", set()) == "cloud:prov-b"

    def test_reload_drops_disabled_provider_entries(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, self._yaml_v1(tmp_path))
        cd = CloudDiscovery(p)
        assert "glm-5" in cd.cloud_models

        v2 = self._yaml_v1(tmp_path)
        v2["providers"]["prov-a"]["enabled"] = False
        _write_yaml(p, v2)
        cd.reload()
        reg = cd.cloud_models
        assert "glm-5" not in reg and "prov-a/glm-5" not in reg
        assert "k3" in reg  # enabled provider 不受影响

    def test_reload_registers_new_provider_floor(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        v1 = {"providers": {"prov-b": self._yaml_v1(tmp_path)["providers"]["prov-b"]}}
        _write_yaml(p, v1)
        cd = CloudDiscovery(p)

        # YAML 编辑：新增 prov-c（spec + 白名单）
        v2 = dict(v1)
        v2["providers"] = dict(v1["providers"])
        v2["providers"]["prov-c"] = {
            "api_key": "sk-c",
            "openai_base": "http://127.0.0.1:1",
            "timeout": 1,
            "enabled_models": ["new-wl"],
            "models": {"new-spec": {"model_id": "new-spec"}},
        }
        _write_yaml(p, v2)
        cd.reload()
        reg = cd.cloud_models
        # 新 provider 的 spec + 白名单 floor 均注册，免抓取可路由
        assert "new-spec" in reg and "prov-c/new-spec" in reg
        assert "new-wl" in reg and "prov-c/new-wl" in reg
        assert cd.resolve_route("new-wl", set()) == "cloud:prov-c"

    def test_reload_restarts_polling(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "poller": {
                    "api_key": "sk-x",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "discovery": {"enabled": True, "interval": 3600},
                }
            }
        })
        cd = CloudDiscovery(p)
        try:
            cd.reload()
            # SIGHUP 链路修复：reload 后轮询线程存活（现状 RED — stop 后无人重启）
            assert cd._poll_thread is not None
            assert cd._poll_thread.is_alive()
        finally:
            cd.stop_polling()

    def test_reload_double_start_polling_noop(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "poller": {
                    "api_key": "sk-x",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "discovery": {"enabled": True, "interval": 3600},
                }
            }
        })
        cd = CloudDiscovery(p)
        try:
            cd.reload()
            t1 = cd._poll_thread
            cd.start_polling()  # handler 的显式调用 — 应为 no-op
            assert cd._poll_thread is t1
        finally:
            cd.stop_polling()

    def test_reload_corrupt_yaml_keeps_registry(self, tmp_path):
        """cross-review I-1：损坏 YAML 的 reload 不得破坏性清空注册表。

        612a47f 的回归：reload 先清 _providers，_load_config 解析失败后
        _providers 保持空 → 严格 floor 调和把 _cloud_models 清成 {} → 全部
        云模型 404 且轮询无法重启（0 providers），比事故前（corrupt reload
        保留旧注册表照常工作）更糟。与 discover_all 的瞬态守卫对齐：
        providers 空/载入失败 → 保旧 + 告警。
        """
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, self._yaml_v1(tmp_path))
        cd = CloudDiscovery(p)
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"
        reg_before = dict(cd.cloud_models)

        p.write_text("providers: [unclosed")  # YAML 解析失败
        try:
            cd.reload()
        finally:
            cd.stop_polling()
        assert cd._config_corrupt is True
        # 注册表存活 — corrupt reload 是意外，不是显式用户移除动作
        assert cd.cloud_models == reg_before
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"
        assert cd.resolve_route("k3", set()) == "cloud:prov-b"

    def test_reload_missing_file_keeps_registry(self, tmp_path):
        """文件缺失（意外删除）与 corrupt 同类 — 保旧，不清空注册表。"""
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, self._yaml_v1(tmp_path))
        cd = CloudDiscovery(p)
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"

        p.unlink()
        try:
            cd.reload()
        finally:
            cd.stop_polling()
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"
        assert cd.resolve_route("k3", set()) == "cloud:prov-b"

    def test_reload_valid_empty_config_publishes_empty_registry(self, tmp_path):
        """安全复评：valid-empty（用户显式删光所有 provider）≠ corrupt —
        显式移除动作应即时生效（用户裁决：移除唯一途径 = 显式用户动作），
        如实发布空注册表，不保旧。
        """
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, self._yaml_v1(tmp_path))
        cd = CloudDiscovery(p)
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"

        _write_yaml(p, {"providers": {}})
        try:
            cd.reload()
        finally:
            cd.stop_polling()
        assert cd._config_corrupt is False
        assert cd.cloud_models == {}
        assert cd.resolve_route("glm-5", set()) is None
        assert cd.resolve_route("k3", set()) is None
        # 关联状态一并清理
        assert cd._candidates == {} and cd._known_absent == {}

    def test_reload_shrunk_whitelist_resets_known_absent(
            self, tmp_path, monkeypatch):
        """cross-review M-4：reload 缩减存活 provider 白名单时 _known_absent
        同步收敛 — 否则用户取消勾选再经 YAML 重新勾选后，新一轮缺席转态
        被陈旧的 absent 集合吞掉（不再告警）。
        """
        from inferfabric import cloud_discovery as cdm

        class _Resp:
            def read(self):
                return b'{"data": [{"id": "other-model"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(cdm.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["glm-5", "k3"],
                }
            }
        })
        cd = CloudDiscovery(p)
        cd.discover_all()  # 两者均缺席成功抓取 → absent = {glm-5, k3}
        assert cd._known_absent.get("prov-a") == {"glm-5", "k3"}

        # YAML 取消勾选 glm-5（保留 k3）→ reload 收敛 absent
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["k3"],
                }
            }
        })
        try:
            cd.reload()
        finally:
            cd.stop_polling()
        assert cd._known_absent.get("prov-a") == {"k3"}
        # 用户显式移除 — 注册表同步撤销
        assert cd.resolve_route("glm-5", set()) is None
        assert cd.resolve_route("k3", set()) == "cloud:prov-a"


class TestShortNameCollisionContract:
    """7.9.1 跨 provider 短名碰撞契约（与发现路径 :334-336 行为对齐）。

    契约：配置序首 provider 赢短键（first-wins，发现/spec/白名单三路径一致）；
    碰撞时本 provider 的前缀键 <provider>/<mid> 仍注册（发现路径既有行为 —
    前缀键无条件写）。注意 resolve_route 短名查找优先（:546）：前缀键不是
    消歧通道 — 短名本身跨 provider 有歧义时按短键 owner 路由（既有语义，
    本 PR 不改 resolve_route/转发核心路径）。
    """

    def test_short_name_collision_first_provider_wins(self, tmp_path, monkeypatch):
        from inferfabric import cloud_discovery as cdm

        class _Resp:
            def read(self):
                return b'{"data": [{"id": "glm-5"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(cdm.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "https://a.example.com/v1",
                    "enabled_models": ["glm-5"],
                },
                "prov-b": {
                    "api_key": "sk-b",
                    "openai_base": "http://127.0.0.1:1",  # dead — 白名单 bare 兜底
                    "timeout": 1,
                    "enabled_models": ["glm-5"],
                },
            }
        })
        cd = CloudDiscovery(p)
        reg = cd.discover_all()
        # 短键归配置序首 provider（发现条目）
        assert reg["glm-5"].provider == "prov-a"
        assert reg["glm-5"].discovered_at > 0
        # 双前缀键俱在（B 经白名单 floor 前缀键注册 — 对齐发现路径碰撞契约）
        assert "prov-a/glm-5" in reg
        assert "prov-b/glm-5" in reg
        assert reg["prov-b/glm-5"].provider == "prov-b"
        # 路由按短键 owner（既有短名优先语义）
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"

    def test_spec_does_not_override_discovered_short_key(self, tmp_path, monkeypatch):
        from inferfabric import cloud_discovery as cdm

        class _Resp:
            def read(self):
                return b'{"data": [{"id": "glm-5"}]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(cdm.urllib.request, "urlopen",
                            lambda req, timeout=None: _Resp())
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "https://a.example.com/v1",
                    "enabled_models": ["glm-5"],
                },
                "prov-b": {
                    "api_key": "sk-b",
                    "openai_base": "https://b.example.com/v1",
                    "discovery": {"enabled": False},
                    "models": {"glm-5": {"model_id": "glm-5"}},
                },
            }
        })
        cd = CloudDiscovery(p)
        reg = cd.discover_all()
        # spec 不覆盖发现短键（spec 路径 :695 first-wins 既有行为）
        assert reg["glm-5"].provider == "prov-a"
        # spec 路径碰撞时整条跳过（含前缀键）— 既有 skip 行为，本 PR 不动 spec 路径
        assert "prov-b/glm-5" not in reg

    def test_whitelist_bare_does_not_override_spec_short_key(self, tmp_path):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "https://a.example.com/v1",
                    "discovery": {"enabled": False},
                    "models": {"glm-5": {"model_id": "glm-5"}},
                },
                "prov-b": {
                    "api_key": "sk-b",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["glm-5"],
                },
            }
        })
        cd = CloudDiscovery(p)
        reg = cd.cloud_models
        # 短键归 spec provider（构造顺序：spec 先注册）
        assert reg["glm-5"].provider == "prov-a"
        # 白名单 bare 不覆盖短键，但前缀键注册（对齐发现路径碰撞契约）
        assert "prov-b/glm-5" in reg
        assert reg["prov-b/glm-5"].provider == "prov-b"

    def test_reload_drops_unchecked_whitelist_model(self, tmp_path):
        """安全加固（review F-1）：YAML 取消勾选（provider 仍启用）→ reload 即撤销。

        缺陷：reload 只加不减 — 被取消勾选的模型滞留可路由直到下一轮轮询。
        floor 不变式：enabled provider 的条目恰好 = specs ∪ 白名单。
        """
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["glm-5", "k3"],
                }
            }
        })
        cd = CloudDiscovery(p)
        assert "glm-5" in cd.cloud_models and "k3" in cd.cloud_models
        try:
            v2 = {
                "providers": {
                    "prov-a": {
                        "api_key": "sk-a",
                        "openai_base": "http://127.0.0.1:1",
                        "timeout": 1,
                        "enabled_models": ["k3"],
                    }
                }
            }
            _write_yaml(p, v2)
            cd.reload()
            reg = cd.cloud_models
            assert "glm-5" not in reg and "prov-a/glm-5" not in reg
            assert cd.resolve_route("glm-5", set()) is None
            assert "k3" in reg  # 未撤销的白名单模型保留
        finally:
            cd.stop_polling()

    def test_reload_drops_removed_spec_model(self, tmp_path):
        """安全加固（review F-1）：YAML 删除 spec（provider 仍启用）→ reload 即撤销。"""
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "discovery": {"enabled": False},
                    "models": {"x1": {"model_id": "x1"}, "x2": {"model_id": "x2"}},
                }
            }
        })
        cd = CloudDiscovery(p)
        assert "x1" in cd.cloud_models and "x2" in cd.cloud_models
        try:
            v2 = {
                "providers": {
                    "prov-a": {
                        "api_key": "sk-a",
                        "openai_base": "http://127.0.0.1:1",
                        "timeout": 1,
                        "discovery": {"enabled": False},
                        "models": {"x2": {"model_id": "x2"}},
                    }
                }
            }
            _write_yaml(p, v2)
            cd.reload()
            reg = cd.cloud_models
            assert "x1" not in reg and "prov-a/x1" not in reg
            assert cd.resolve_route("x1", set()) is None
            assert "x2" in reg
        finally:
            cd.stop_polling()


class TestPublishReconciliation:
    """安全加固（review F-2）：discover_all 发布临界区对账当前配置。

    闭合与 reload 的并发竞态：抓取在锁外、耗时可达分钟级 — 期间配置可能
    已变。发布时按当前 _providers 过滤（不复活已删/禁 provider 的条目）+
    重跑 floor（补注册新增/启用 provider 的 floor）。
    """

    def test_publish_does_not_resurrect_provider_disabled_during_fetch(
            self, tmp_path, monkeypatch):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["glm-5"],
                }
            }
        })
        cd = CloudDiscovery(p)
        assert "glm-5" in cd.cloud_models  # 启动 floor

        def _fake_discover(cfg):
            # 模拟抓取期间并发 reload 禁用了本 provider
            cd._providers["prov-a"].enabled = False
            return [CloudModel(model_id="glm-5", provider="prov-a",
                               discovered_at=time.time())]

        monkeypatch.setattr(cd, "_discover_provider", _fake_discover)
        reg = cd.discover_all()
        # 发布时不复活被禁 provider 的条目（严格 floor：发布空集）
        assert "glm-5" not in reg
        assert cd.resolve_route("glm-5", set()) is None

    def test_publish_registers_floor_of_provider_enabled_during_fetch(
            self, tmp_path, monkeypatch):
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["a1"],
                },
                "prov-b": {
                    "api_key": "sk-b",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled": False,
                    "enabled_models": ["b1"],
                },
            }
        })
        cd = CloudDiscovery(p)
        assert "b1" not in cd.cloud_models  # 启动时 prov-b 禁用 → 无 floor

        def _fake_discover(cfg):
            # 模拟抓取期间并发 reload 启用了 prov-b
            cd._providers["prov-b"].enabled = True
            return []

        monkeypatch.setattr(cd, "_discover_provider", _fake_discover)
        reg = cd.discover_all()
        # 发布时补注册新启用 provider 的 floor
        assert "b1" in reg
        assert cd.resolve_route("b1", set()) == "cloud:prov-b"
        assert "a1" in reg  # 原有 floor 不受影响

    def test_publish_does_not_resurrect_model_unchecked_during_fetch(
            self, tmp_path, monkeypatch):
        """cross-review I-2：抓取窗口内用户取消勾选 + reload 完成 — 发布时
        per-model 对账当前 floor，不复活已撤销的模型（F-2 的 per-model 残留）。

        抓取按 OLD cfg.enabled_models 过滤（:346），耗时可达分钟级；发布若
        只查 provider 存活+enabled，取消勾选要等下一轮轮询（≤1h）才生效 —
        违反核心不变量（discover_all 后条目 ≡ 当前 floor）。
        """
        p = tmp_path / "cloud_provider.yaml"
        _write_yaml(p, {
            "providers": {
                "prov-a": {
                    "api_key": "sk-a",
                    "openai_base": "http://127.0.0.1:1",
                    "timeout": 1,
                    "enabled_models": ["glm-5"],
                }
            }
        })
        cd = CloudDiscovery(p)
        assert cd.resolve_route("glm-5", set()) == "cloud:prov-a"

        def _fake_discover(cfg):
            # 模拟抓取期间（锁外、分钟级）用户 YAML 取消勾选 + reload 完成
            _write_yaml(p, {
                "providers": {
                    "prov-a": {
                        "api_key": "sk-a",
                        "openai_base": "http://127.0.0.1:1",
                        "timeout": 1,
                        "enabled_models": [],
                    }
                }
            })
            cd.reload()
            # 抓取结果按旧白名单过滤 — 含已撤销的 glm-5
            return [CloudModel(model_id="glm-5", provider="prov-a",
                               discovered_at=time.time())]

        monkeypatch.setattr(cd, "_discover_provider", _fake_discover)
        try:
            reg = cd.discover_all()
        finally:
            cd.stop_polling()
        # 发布时按当前 per-model floor 对账 — 不复活已取消勾选的模型
        assert "glm-5" not in reg and "prov-a/glm-5" not in reg
        assert cd.resolve_route("glm-5", set()) is None
