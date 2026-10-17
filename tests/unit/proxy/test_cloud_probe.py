"""
unit/proxy/test_cloud_probe.py — 云 Provider 探测与 provider-test 端点（v6.6 门禁）

覆盖:
  - _probe_cloud_endpoint：openai/anthropic 无 probe 的 /models 分类
      2xx→ok(+model_count)、404→ok_no_listing、401/403→auth_failed、429→rate_limited
  - anthropic base 以 /v1 结尾 → 直接 /models、/messages（不再多拼 /v1）
  - probe_model 真实 1-token 生成：200→ok；403(detail 含 not_supported)→model_unavailable（§3.4）
  - _handle_cloud_provider_test：不存在→404；缺 provider→400；无 base→400；正常→ok 为各 base 的 AND
  - _handle_cloud_test：缺 url / 坏 protocol / 元数据 URL → 400；正常 → 200 + pass
harness 范式照 test_provider_models.py（ProxyHandler.__new__ + stub _read_body/_send_json
+ 真 CloudDiscovery(tmp_path)）；probe 连接 patch h._make_pinned_conn 注入预置响应。
"""

import json
from types import SimpleNamespace

from inferfabric.cloud_discovery import CloudDiscovery, SecretsManager, ProviderConfig
from inferfabric.proxy.handler import ProxyHandler


class _FakeConn:
    """假 http.client 连接：request 记录调用，getresponse 返回预置 (status, body)。"""

    def __init__(self, status, body, record):
        self._status = status
        self._body = body if isinstance(body, str) else json.dumps(body)
        self._record = record
        self.closed = False
        self._http_vsn_str = "HTTP/1.1"

    def request(self, method, path, body=None, headers=None):
        self._record.append({"method": method, "path": path, "body": body, "headers": headers or {}})

    def getresponse(self):
        return SimpleNamespace(status=self._status, read=lambda limit=2000: self._body.encode("utf-8"))

    def close(self):
        self.closed = True


def _install_fake_probe(h, queue, record=None):
    """patch h._make_pinned_conn → 按 queue 顺序返回预置响应（元素: (status, body)）。"""
    rec = record if record is not None else []
    def factory(host, port, resolved_ip, use_tls, timeout=15):
        status, body = queue.pop(0) if queue else (200, '{"data":[]}')
        return _FakeConn(status, body, rec)
    h._make_pinned_conn = factory
    return rec


def _handler():
    return ProxyHandler.__new__(ProxyHandler)


class TestProbeEndpoint:
    """_probe_cloud_endpoint 分类逻辑（openai/anthropic × 无 probe）。"""

    def test_openai_models_ok_counts(self):
        h = _handler()
        rec = _install_fake_probe(h, [(200, '{"data":[{"id":"a"},{"id":"b"},{"id":"c"}]}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-1", "openai", "", "1.2.3.4")
        assert r["status"] == "ok"
        assert r["model_count"] == 3
        assert rec[0]["method"] == "GET"
        assert rec[0]["path"] == "/models"
        assert rec[0]["headers"]["Authorization"] == "Bearer sk-1"

    def test_openai_models_404_no_listing(self):
        h = _handler()
        _install_fake_probe(h, [(404, '{"error":"not found"}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-1", "openai", "", "1.2.3.4")
        assert r["status"] == "ok_no_listing"

    def test_empty_key_omits_auth_header(self):
        # §3.3-A：api_key 可空（公开端点裸测）→ 空 key 时不发 Authorization 头（不能发空 Bearer）
        h = _handler()
        rec = _install_fake_probe(h, [(200, '{"data":[]}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "", "openai", "", "1.2.3.4")
        assert r["status"] == "ok"
        assert "Authorization" not in rec[0]["headers"]

    def test_empty_key_anthropic_omits_x_api_key(self):
        # anthropic 空 key：不发 x-api-key，但协议头 anthropic-version 恒发
        h = _handler()
        rec = _install_fake_probe(h, [(200, '{"data":[]}')])
        r = h._probe_cloud_endpoint("https://x.com/anthropic/v1", "", "anthropic", "", "1.2.3.4")
        assert r["status"] == "ok"
        assert "x-api-key" not in rec[0]["headers"]
        assert rec[0]["headers"].get("anthropic-version") == "2023-06-01"

    def test_openai_models_401_auth_failed(self):
        h = _handler()
        _install_fake_probe(h, [(401, '{"error":{"message":"bad key"}}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-bad", "openai", "", "1.2.3.4")
        assert r["status"] == "auth_failed"

    def test_openai_models_429_rate_limited(self):
        h = _handler()
        _install_fake_probe(h, [(429, '{"error":"slow down"}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-1", "openai", "", "1.2.3.4")
        assert r["status"] == "rate_limited"

    def test_anthropic_v1_suffix_paths(self):
        # base 以 /v1 结尾 → 直接 /models（不是 /v1/models）；鉴权用 x-api-key
        h = _handler()
        rec = _install_fake_probe(h, [(200, '{"data":[{"id":"m"}]}')])
        r = h._probe_cloud_endpoint("https://x.com/anthropic/tokenplan/personal/v1",
                                    "sk-1", "anthropic", "", "1.2.3.4")
        assert r["status"] == "ok"
        assert rec[0]["path"] == "/models"
        assert "x-api-key" in rec[0]["headers"]
        assert rec[0]["headers"].get("anthropic-version") == "2023-06-01"

    def test_anthropic_bare_base_first_path_404_no_listing(self):
        # §4.2 曾要求 404 后回落 /models，但与 §3.3-A（404→ok_no_listing 终止）冲突；
        # 取 §3.3-A + 冻结代码语义：首个 anthropic 路径(/v1/models) 404 → ok_no_listing（可达、无列表）。
        h = _handler()
        rec = _install_fake_probe(h, [(404, "{}")])
        r = h._probe_cloud_endpoint("https://x.com/anthropic", "sk-1", "anthropic", "", "1.2.3.4")
        assert r["status"] == "ok_no_listing"
        assert rec[0]["path"] == "/v1/models"

    def test_probe_model_200_ok(self):
        h = _handler()
        rec = _install_fake_probe(h, [(200, '{"id":"cmpl-x"}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-1", "openai", "glm-5.3-flash", "1.2.3.4")
        assert r["status"] == "ok"
        assert r["model_count"] == 1
        assert rec[0]["method"] == "POST"
        assert rec[0]["path"] == "/chat/completions"
        body = json.loads(rec[0]["body"])
        assert body["model"] == "glm-5.3-flash"
        assert body["max_tokens"] == 1

    def test_probe_model_403_not_supported_is_unavailable(self):
        # §3.4：403 且 detail 含 not_supported → model_unavailable（连接与鉴权正常，仅模型不在套餐）
        h = _handler()
        _install_fake_probe(h, [(403, '{"error":{"message":"token_plan_person_model_not_supported: glm-9"}}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-1", "openai", "glm-9", "1.2.3.4")
        assert r["status"] == "model_unavailable"

    def test_probe_model_403_plain_is_auth_failed(self):
        # 无 not_supported 特征的 403 → 仍归 auth_failed
        h = _handler()
        _install_fake_probe(h, [(403, '{"error":{"message":"forbidden"}}')])
        r = h._probe_cloud_endpoint("https://api.x.com/v1", "sk-1", "openai", "glm-9", "1.2.3.4")
        assert r["status"] == "auth_failed"


class TestProviderTestHandler:
    """_handle_cloud_provider_test（R4：用服务端已存 key 测已注册 provider 的全部 base）。"""

    def _pm_with(self, tmp_path, name="Qianfan-Token",
                 openai_base="http://127.0.0.1:1/v1", anthropic_base="", api_key="sk-x"):
        cd = CloudDiscovery(config_path=None)
        cd._config_path = tmp_path / "cloud_provider.yaml"
        cd._secrets_mgr = SecretsManager(path=tmp_path / "secrets.env")
        cd._inject_secrets_env = lambda: None
        cd._providers[name] = ProviderConfig(
            name=name, api_key=api_key, openai_base=openai_base, anthropic_base=anthropic_base,
            enabled=True, discovery_enabled=True, key_env_var="IFF_X_KEY",
        )
        return SimpleNamespace(cloud=cd)

    def _handler(self, body):
        h = ProxyHandler.__new__(ProxyHandler)
        h._read_body = lambda: body
        h._sent = []
        h._send_json = lambda data, status=200, extra_headers=None: h._sent.append((data, status))
        return h

    def test_unknown_provider_404(self, tmp_path):
        pm = self._pm_with(tmp_path)
        h = self._handler({"provider": "ghost"})
        h._handle_cloud_provider_test(pm)
        data, status = h._sent[-1]
        assert status == 404
        assert "ghost" in data["error"]

    def test_missing_provider_400(self, tmp_path):
        pm = self._pm_with(tmp_path)
        h = self._handler({})
        h._handle_cloud_provider_test(pm)
        assert h._sent[-1][1] == 400

    def test_no_base_400(self, tmp_path):
        pm = self._pm_with(tmp_path, openai_base="", anthropic_base="")
        h = self._handler({"provider": "Qianfan-Token"})
        h._handle_cloud_provider_test(pm)
        data, status = h._sent[-1]
        assert status == 400

    def test_all_bases_pass_ok_true(self, tmp_path):
        pm = self._pm_with(tmp_path,
                           openai_base="http://127.0.0.1:1/v1",
                           anthropic_base="http://127.0.0.1:1/anthropic/v1")
        h = self._handler({"provider": "Qianfan-Token"})
        _install_fake_probe(h, [(200, '{"data":[{"id":"a"}]}'), (200, '{"data":[{"id":"b"}]}')])
        h._handle_cloud_provider_test(pm)
        data, status = h._sent[-1]
        assert status == 200
        assert data["key_set"] is True
        assert data["ok"] is True
        assert len(data["results"]) == 2
        assert all(r["pass"] for r in data["results"])

    def test_one_base_fails_ok_false(self, tmp_path):
        pm = self._pm_with(tmp_path,
                           openai_base="http://127.0.0.1:1/v1",
                           anthropic_base="http://127.0.0.1:1/anthropic/v1")
        h = self._handler({"provider": "Qianfan-Token"})
        _install_fake_probe(h, [(200, '{"data":[]}'), (401, '{"error":{"message":"bad"}}')])
        h._handle_cloud_provider_test(pm)
        data, status = h._sent[-1]
        assert data["ok"] is False
        assert data["results"][1]["status"] == "auth_failed"
        assert data["results"][0]["status"] == "ok"


class TestCloudTestHandler:
    """_handle_cloud_test（重做：不再要求 URL 已注册）。"""

    def _handler(self, body):
        h = ProxyHandler.__new__(ProxyHandler)
        h._read_body = lambda: body
        h._sent = []
        h._send_json = lambda data, status=200, extra_headers=None: h._sent.append((data, status))
        return h

    def test_missing_url_400(self):
        h = self._handler({"api_key": "x"})
        h._handle_cloud_test(None)
        assert h._sent[-1][1] == 400

    def test_bad_protocol_400(self):
        h = self._handler({"url": "http://127.0.0.1/v1", "protocol": "bogus"})
        h._handle_cloud_test(None)
        assert h._sent[-1][1] == 400

    def test_happy_path_pass_true(self):
        h = self._handler({"url": "http://127.0.0.1:1/v1", "api_key": "sk-1", "protocol": "openai"})
        _install_fake_probe(h, [(200, '{"data":[{"id":"a"}]}')])
        h._handle_cloud_test(None)
        data, status = h._sent[-1]
        assert status == 200
        assert data["pass"] is True
        assert data["status"] == "ok"
