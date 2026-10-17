"""
unit/infra/test_ssrf.py — 云 Provider 测试 URL 校验（v6.6 放宽策略）

测试对象: inferfabric.proxy.handler.ProxyHandler._validate_cloud_test_url
新策略（管理员端点，测试的意义是"加之前先测"，故不再要求 URL 已注册）：
  - http/https 均放行（http 给内网中转站）；ftp 等其它 scheme 拒绝
  - 私有 / 回环 / 链路本地 IP 字面量，以及域名解析到的私有 IP，全部放行（内网中转场景），记 warning
  - 硬拦：云元数据地址 169.254.169.254（IP 字面量与域名两种形态）、DNS 解析失败、非法 URL、非 http(s) scheme
  - 已删除"必须是已注册 provider 主机"白名单（没注册才需要测）
覆盖 _handle_cloud_test 的 URL 门禁（400）。
"""

import socket
from unittest.mock import patch

from inferfabric.proxy.handler import ProxyHandler


class FakeServer:
    """Minimal fake server for ProxyHandler."""
    def __init__(self, proxy_mgr=None):
        self.proxy_mgr = proxy_mgr


def _make_dns_mock(ip_map):
    """Return a mock for socket.getaddrinfo.

    ip_map: {hostname: [ip_str, ...]}. Unmapped hostnames resolve to public 1.2.3.4.
    """
    def _getaddrinfo(host, *args, **kwargs):
        ips = ip_map.get(host, ["1.2.3.4"])
        results = []
        for ip in ips:
            family = 10 if ":" in ip else 2
            results.append((family, 1, 0, "", (ip, 0)))
        return results
    return _getaddrinfo


def _handler():
    """ProxyHandler with a no-op pm (new validator no longer touches the registry)."""
    h = ProxyHandler.__new__(ProxyHandler)
    h.server = FakeServer(proxy_mgr=None)
    return h


class TestValidateCloudTestUrlNewPolicy:
    """_validate_cloud_test_url under the relaxed (intranet relay) policy."""

    # ── IP 字面量：私有 / 回环 / 链路本地放行 ──
    def test_private_ip_127_allowed(self):
        h = _handler()
        valid, reason, ips = h._validate_cloud_test_url("http://127.0.0.1:8999/v1", None)
        assert valid, reason
        assert ips == ["127.0.0.1"]

    def test_private_ip_10_allowed(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("https://10.0.0.1/models", None)
        assert valid

    def test_private_ip_172_16_allowed(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("https://172.16.0.1/models", None)
        assert valid

    def test_private_ip_192_168_allowed(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("http://192.168.1.10:3000/v1", None)
        assert valid

    def test_link_local_non_metadata_allowed(self):
        # 169.254.1.1 是链路本地但非元数据地址 → 放行（仅 169.254.169.254 硬拦）
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("http://169.254.1.1/models", None)
        assert valid

    def test_ipv6_loopback_allowed(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("https://[::1]/models", None)
        assert valid

    def test_ipv6_private_allowed(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("https://[fc00::1]/models", None)
        assert valid

    # ── 云元数据地址硬拦（IP 字面量 + 域名两种形态） ──
    def test_metadata_ip_literal_rejected(self):
        h = _handler()
        valid, reason, _ = h._validate_cloud_test_url("http://169.254.169.254/latest/meta-data/", None)
        assert not valid
        assert "metadata" in reason.lower()

    def test_metadata_ip_domain_rejected(self):
        h = _handler()
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                  side_effect=_make_dns_mock({"meta.internal": ["169.254.169.254"]})) as m:
            valid, reason, _ = h._validate_cloud_test_url("http://meta.internal/latest/meta-data/", None)
        assert not valid

    # ── scheme：http 放行，ftp 拒绝 ──
    def test_http_scheme_allowed(self):
        h = _handler()
        # 用公网 IP 字面量隔离 scheme 判定，避免触网
        valid, _, _ = h._validate_cloud_test_url("http://1.2.3.4/v1", None)
        assert valid

    def test_ftp_scheme_rejected(self):
        h = _handler()
        valid, reason, _ = h._validate_cloud_test_url("ftp://1.2.3.4/v1", None)
        assert not valid
        assert "http/https" in reason.lower()

    # ── 白名单已删除：公网域名 / 解析到私有 IP 均放行 ──
    def test_public_domain_allowed(self):
        h = _handler()
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                  side_effect=_make_dns_mock({"api.openai.com": ["1.2.3.4"]})) as m:
            valid, _, _ = h._validate_cloud_test_url("https://api.openai.com/v1", None)
        assert valid

    def test_dns_private_resolution_allowed(self):
        # 曾拦的 DNS-rebinding 场景，新策略放行（管理员端点，内网中转）
        h = _handler()
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                  side_effect=_make_dns_mock({"relay.internal": ["10.0.0.5"]})) as m:
            valid, _, ips = h._validate_cloud_test_url("http://relay.internal/v1", None)
        assert valid
        assert "10.0.0.5" in ips

    # ── 保留：非法 URL / 空 / DNS 失败 ──
    def test_valid_url_with_path(self):
        h = _handler()
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                  side_effect=_make_dns_mock({"api.anthropic.com": ["1.2.3.4"]})) as m:
            valid, _, _ = h._validate_cloud_test_url("https://api.anthropic.com/v1/models", None)
        assert valid

    def test_invalid_url_malformed(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("not-a-url", None)
        assert not valid

    def test_empty_url(self):
        h = _handler()
        valid, _, _ = h._validate_cloud_test_url("", None)
        assert not valid

    def test_dns_resolution_failure(self):
        h = _handler()
        with patch("inferfabric.proxy.handler.socket.getaddrinfo",
                  side_effect=socket.gaierror("DNS failed")) as m:
            valid, reason, _ = h._validate_cloud_test_url("https://api.openai.com/v1", None)
        assert not valid


class TestHandleCloudTestUrlGate:
    """_handle_cloud_test 的 URL 门禁（放宽策略下：元数据/ftp → 400）。"""

    def _handler_body(self, body):
        h = _handler()
        h._read_body = lambda: body
        h._sent = []
        h._send_json = lambda data, status=200, extra_headers=None: h._sent.append((data, status))
        return h

    def test_metadata_url_400(self):
        h = self._handler_body({"url": "http://169.254.169.254/latest/meta-data/"})
        h._handle_cloud_test(None)
        data, status = h._sent[-1]
        assert status == 400
        assert "URL check failed" in str(data)

    def test_ftp_url_400(self):
        h = self._handler_body({"url": "ftp://1.2.3.4/v1"})
        h._handle_cloud_test(None)
        data, status = h._sent[-1]
        assert status == 400
        assert "URL check failed" in str(data)
