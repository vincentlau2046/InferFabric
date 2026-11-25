"""本机健康检查禁用系统代理（issue #56）。

背景：macOS 系统代理（FlClash 7890）开启且 bypass 不含 localhost 时，
`urlopen("http://localhost:<port>")` 被转发到远端代理节点 —— 健康检查
3s×3 次全超时，snapshot 冷路径 17-28s，dashboard 周期性卡死；隔离验收
网关把 `localhost:8007` 误连到代理节点自己的 8007（读挂起）。

契约：
- `_is_local_url`：localhost / 127.0.0.1 / ::1 / [::1] → True；其余 False。
- `check_http_status` / `wait_http` 对本机目标用 no-proxy opener：
  即使 http_proxy 指向死端口，本机活端口仍 ✅（真实 HTTP server 验证）。
- 非本机 URL 行为不变（仍走系统代理语义）。
"""

import http.server
import os
import threading

import pytest

from inferfabric.health import _is_local_url, check_http_status, wait_http


@pytest.mark.parametrize("url", [
    "http://localhost:8007/v1/models",
    "http://127.0.0.1:8999/health",
    "http://[::1]:8007/health",
    "http://localhost:11434/api/tags",
])
def test_is_local_url_true(url):
    assert _is_local_url(url) is True


@pytest.mark.parametrize("url", [
    "https://api.github.com/zen",
    "http://atlaswork.example.com/v1/models",
    "http://192.168.1.10:8000/health",   # 局域网非本机 — 不豁免
    "not a url",
])
def test_is_local_url_false(url):
    assert _is_local_url(url) is False


@pytest.fixture()
def local_http_server():
    """本机活端口 HTTP server（返回 200 JSON）。"""
    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_check_http_status_bypasses_dead_proxy(local_http_server, monkeypatch):
    """http_proxy 指向死端口时，本机 localhost 检查必须仍 ✅ ——
    修复前该用例经代理必失败（issue #56 的复现器）。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")     # 死端口（discard）
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert check_http_status(f"http://localhost:{local_http_server}/",
                             timeout=2, retries=1) == "✅"
    assert check_http_status(f"http://127.0.0.1:{local_http_server}/",
                             timeout=2, retries=1) == "✅"


def test_wait_http_bypasses_dead_proxy(local_http_server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert wait_http(f"http://localhost:{local_http_server}/", timeout=5) is True


def test_non_local_url_still_uses_proxy_env(local_http_server, monkeypatch):
    """非本机 URL 语义不变：死代理下必须失败（证明我们没全局关代理）。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert check_http_status("http://192.0.2.1:1/health",
                             timeout=2, retries=1) == "❌"
