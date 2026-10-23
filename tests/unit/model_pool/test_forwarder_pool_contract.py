"""终审修复 pass — Phase E：forwarder 池传输契约。

覆盖：
  #10 forward_anthropic_local suppress 模式返回 CloudResult（429/5xx/4xx/
      连接失败 → committed=False 不写客户端；200 → committed=True），
      200 非流式 committed 先于 body 读取（读取失败 = 未提交）
  #12 _retry_after_seconds 支持 HTTP-date 格式
  #7  forward_to_cloud / forward_anthropic_local 剩余预算 timeout 传导
  （默认路径回归由既有 tests/unit/infra/test_forwarder.py 36 例证明）
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import pytest
from inferfabric.cloud_discovery import CloudModel, ProviderConfig
from inferfabric.forwarder import (
    CloudResult,
    _retry_after_seconds,
    forward_anthropic_local,
    forward_to_cloud,
)


class _WFile:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class _FakeHandler:
    """最小 handler 替身：send_json 写入 wfile（可断言「未写客户端」）。"""

    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.sent_headers = {}
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = str(v)

    def end_headers(self):
        pass


def _make_model(port=8000, served="qwen-small"):
    m = MagicMock()
    m.name = "qwen-small-real"
    m.port = port
    m.served_name = served
    return m


def _resp(status, body=b'{"content":[{"type":"text","text":"hi"}],"usage":{"input_tokens":1,"output_tokens":2}}',
          headers=None, read_error=None):
    r = MagicMock()
    r.status = status
    r.read.side_effect = read_error or (lambda: body)
    r.getheader = lambda h, default=None: (headers or {}).get(h, default)
    r.headers = headers or {}
    return r


# ── #10 forward_anthropic_local suppress 契约 ──

class TestAnthropicLocalSuppressContract:

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_429_returns_uncommitted_cloudresult_with_retry_after(self, mock_conn):
        resp = _resp(429, headers={"Retry-After": "7"})
        mock_conn.return_value.getresponse.return_value = resp
        h = _FakeHandler()
        out = forward_anthropic_local(h, MagicMock(), {"messages": []}, "auth",
                                      _make_model(), "iff/fast",
                                      suppress_client_errors=True)
        assert isinstance(out, CloudResult)
        assert out.status == 429 and out.committed is False
        assert out.retry_after == 7.0
        assert h.wfile.data == b""          # 未写客户端
        assert mock_conn.return_value.request.call_count == 1   # 单次尝试不退避

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_5xx_returns_uncommitted_cloudresult(self, mock_conn):
        mock_conn.return_value.getresponse.return_value = _resp(503)
        h = _FakeHandler()
        out = forward_anthropic_local(h, MagicMock(), {"messages": []}, "auth",
                                      _make_model(), "iff/fast",
                                      suppress_client_errors=True)
        assert isinstance(out, CloudResult)
        assert out.status == 503 and out.committed is False
        assert h.wfile.data == b""

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_non_retryable_4xx_returns_uncommitted(self, mock_conn):
        # 404 → committed=False 不写客户端：终止信封由 dispatch 统一发
        mock_conn.return_value.getresponse.return_value = _resp(404)
        h = _FakeHandler()
        out = forward_anthropic_local(h, MagicMock(), {"messages": []}, "auth",
                                      _make_model(), "iff/fast",
                                      suppress_client_errors=True)
        assert isinstance(out, CloudResult)
        assert out.status == 404 and out.committed is False
        assert h.wfile.data == b""

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_200_nonstream_commits_after_body_read(self, mock_conn):
        mock_conn.return_value.getresponse.return_value = _resp(200)
        h = _FakeHandler()
        out = forward_anthropic_local(h, MagicMock(), {"messages": []}, "auth",
                                      _make_model(), "iff/fast",
                                      suppress_client_errors=True)
        assert isinstance(out, CloudResult)
        assert out.status == 200 and out.committed is True
        assert h.wfile.data           # 已交付客户端

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_200_body_read_failure_is_uncommitted(self, mock_conn):
        # committed 先于 body 读取（终审 #10）：读取失败 = 未提交、未写客户端
        mock_conn.return_value.getresponse.return_value = _resp(
            200, read_error=ConnectionResetError("truncated"))
        h = _FakeHandler()
        out = forward_anthropic_local(h, MagicMock(), {"messages": []}, "auth",
                                      _make_model(), "iff/fast",
                                      suppress_client_errors=True)
        assert isinstance(out, CloudResult)
        assert out.committed is False
        assert h.wfile.data == b""

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_conn_refused_returns_uncommitted_none_status(self, mock_conn):
        mock_conn.return_value.request.side_effect = ConnectionRefusedError()
        h = _FakeHandler()
        out = forward_anthropic_local(h, MagicMock(), {"messages": []}, "auth",
                                      _make_model(), "iff/fast",
                                      suppress_client_errors=True)
        assert isinstance(out, CloudResult)
        assert out.status is None and out.committed is False   # 连接层失败
        assert h.wfile.data == b""

    @patch("inferfabric.forwarder.send_json")
    @patch("inferfabric.forwarder.HTTPConnection")
    def test_default_mode_still_returns_int(self, mock_conn, mock_send):
        # 默认路径契约不变：返回 int 状态码（PR-14 回归锚点）
        mock_conn.return_value.getresponse.return_value = _resp(200)
        out = forward_anthropic_local(_FakeHandler(), MagicMock(),
                                      {"messages": []}, "auth",
                                      _make_model(), "iff/fast")
        assert out == 200


# ── #12 Retry-After HTTP-date ──

class TestRetryAfterHttpDate:

    def test_http_date_in_near_future_parsed(self):
        # RFC 7231 HTTP-date：未来 ~45s → 秒数
        future = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                               time.gmtime(time.time() + 45))
        val = _retry_after_seconds({"Retry-After": future})
        assert val is not None and 40 <= val <= 50

    def test_http_date_in_past_is_none(self):
        past = "Wed, 21 Oct 2015 07:28:00 GMT"
        assert _retry_after_seconds({"Retry-After": past}) is None

    def test_numeric_seconds_still_work(self):
        assert _retry_after_seconds({"Retry-After": "12"}) == 12.0

    def test_garbage_is_none(self):
        assert _retry_after_seconds({"Retry-After": "soon"}) is None


# ── #7 剩余预算 timeout 传导 ──

class TestTimeoutPropagation:

    @patch("inferfabric.forwarder.urlopen")
    def test_cloud_timeout_caps_provider_default(self, mock_urlopen):
        mock_urlopen.return_value = _resp(200)
        cfg = ProviderConfig(name="p", api_key="k",
                             openai_base="https://x/v1", timeout=120)
        cm = CloudModel(model_id="m", provider="p",
                        openai_available=True, anthropic_available=True)
        forward_to_cloud(_FakeHandler(), {"messages": []}, cfg, cm,
                         protocol="openai", original_model="iff/fast",
                         suppress_client_errors=True, timeout=12.5)
        assert mock_urlopen.call_args.kwargs["timeout"] == 12.5

    @patch("inferfabric.forwarder.urlopen")
    def test_cloud_default_uses_provider_timeout(self, mock_urlopen):
        mock_urlopen.return_value = _resp(200)
        cfg = ProviderConfig(name="p", api_key="k",
                             openai_base="https://x/v1", timeout=120)
        cm = CloudModel(model_id="m", provider="p",
                        openai_available=True, anthropic_available=True)
        forward_to_cloud(_FakeHandler(), {"messages": []}, cfg, cm,
                         protocol="openai", original_model="iff/fast",
                         suppress_client_errors=True)
        assert mock_urlopen.call_args.kwargs["timeout"] == 120

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_local_timeout_passed_to_connection(self, mock_conn):
        mock_conn.return_value.getresponse.return_value = _resp(200)
        forward_anthropic_local(_FakeHandler(), MagicMock(), {"messages": []},
                                "auth", _make_model(), "iff/fast",
                                suppress_client_errors=True, timeout=9.0)
        assert mock_conn.call_args.kwargs["timeout"] == 9.0

    @patch("inferfabric.forwarder.HTTPConnection")
    def test_local_default_timeout_unchanged(self, mock_conn):
        mock_conn.return_value.getresponse.return_value = _resp(200)
        forward_anthropic_local(_FakeHandler(), MagicMock(), {"messages": []},
                                "auth", _make_model(), "iff/fast")
        assert mock_conn.call_args.kwargs["timeout"] == 300
