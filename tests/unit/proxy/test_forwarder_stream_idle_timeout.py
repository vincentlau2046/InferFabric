"""forwarder committed 流超时与池剩余预算解耦（7.11.4 离线修复）。

根因（2026-11-24 定位 + request_log 独立复核：90 条 error=upstream_http_200
+ tokens_in=0，62 条落 55–95s 时长窗，69 条集中 pool:max/cloud:tathr/gpt-6.2-sol）：
池路径把「deadline 剩余预算」当 urlopen/HTTPConnection 的 timeout 下发——
该参数是 socket 级**任意两次 recv 的最大静默间隔**，不是总时长。上游 headers
秒回（committed=True，§6.4 不可重放）后，大请求 prefill/长推理期的流静默 >
剩余预算 → resp.read() TimeoutError → committed 流截断 → 客户端
「Connection lost mid-response」。

契约（peer 诊断 B 方案，用户 2026-11-24 授权离线改 forwarder）：
- committed 前的 connect/headers 阶段仍用剩余预算（排队堆积防线，语义不变）；
- headers 已到（即将转发客户端）后，socket 超时重置为独立 idle timeout
  （云端 = provider_cfg.timeout，本地 = 既有 300s）；§6.4 语义零变化。

测试用真实本地 HTTP server（headers 即发 + body 延迟 > 剩余预算、< idle
timeout），不 mock socket——超时语义只存在于真实 socket。
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from inferfabric.cloud_discovery import CloudModel, ProviderConfig
from inferfabric.forwarder import CloudResult, forward_anthropic_local, forward_to_cloud

BODY_SILENCE_S = 2.5      # body 静默期：> 剩余预算(1.0s)、< idle timeout(8s)
POOL_REMAINING_S = 1.0    # 池路径传入的「剩余预算」
IDLE_TIMEOUT_S = 8        # provider timeout / 本地 idle


class _WFile:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class _FakeHandler:
    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = str(v)

    def end_headers(self):
        pass


class _SlowBodyHandler(BaseHTTPRequestHandler):
    """headers 立即下发，body 静默 BODY_SILENCE_S 后到达。"""

    def do_POST(self):  # noqa: N802 (http.server 命名)
        _ = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        time.sleep(BODY_SILENCE_S)
        self.wfile.write(b"data: hi\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


def _slow_body_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowBodyHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_cloud_committed_stream_reads_use_idle_timeout_not_remaining():
    srv, t = _slow_body_server()
    try:
        cfg = ProviderConfig(
            name="slow-provider", api_key="sk-test",
            openai_base=f"http://127.0.0.1:{srv.server_address[1]}/v1",
            timeout=IDLE_TIMEOUT_S)
        cm = CloudModel(model_id="m", provider="slow-provider")
        h = _FakeHandler()
        res = forward_to_cloud(
            h, {"model": "m", "stream": True, "messages": []},
            cfg, cm, suppress_client_errors=True, timeout=POOL_REMAINING_S)
        # 旧行为：remaining(1.0s) 静默墙在 2.5s body 前触发 → 截断
        # upstream_stream_failed_after_commit；新行为：committed 后 socket
        # 超时 = provider timeout(8s) → 流完整送达
        assert res.status == 200
        assert res.error is None, res.error
        assert b"data: hi" in h.wfile.data
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_local_committed_stream_survives_silence_longer_than_remaining():
    srv, t = _slow_body_server()
    try:
        model_obj = SimpleNamespace(served_name="m", port=srv.server_address[1],
                                     name="m")
        h = _FakeHandler()
        res = forward_anthropic_local(
            h, None, {"model": "m", "stream": True, "messages": []},
            "", model_obj, "m",
            suppress_client_errors=True, timeout=POOL_REMAINING_S)
        # 旧行为：local_stream_failed_after_commit；新行为：committed 后
        # socket 超时 = 300s idle → 流完整送达
        assert isinstance(res, CloudResult)
        assert res.committed is True
        assert res.error is None, res.error
        assert b"data: hi" in h.wfile.data
    finally:
        srv.shutdown()
        t.join(timeout=2)
